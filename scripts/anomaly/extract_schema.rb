#!/usr/bin/env ruby
# Parse source as data. Never load/eval a Rails file, run a callback, or connect to Mongo.
require 'prism'
require 'active_support/inflector'
require 'json'
require 'digest'
require 'optparse'
require 'pathname'
require 'time'

class SchemaExtractionError < StandardError; end
class UnsupportedLiteral < StandardError; end

class RailsSchemaExtractor
  VERSION = '1'
  STRUCTURAL = %i[field store_in belongs_to has_and_belongs_to_many embeds_many embeds_one embedded_in].freeze
  TYPES = %w[String Integer Float Boolean Mongoid::Boolean Time Date DateTime Hash Array Object BSON::ObjectId BigDecimal BSON::Decimal128].freeze
  TIMESTAMPS = {'Mongoid::Timestamps' => %w[created_at updated_at],
                'Mongoid::Timestamps::Created' => %w[created_at],
                'Mongoid::Timestamps::Updated' => %w[updated_at]}.freeze

  def initialize(root, commit:, repository:, ref:)
    @root = Pathname.new(root).realpath
    @provenance = {'rails_commit' => commit, 'repository' => repository, 'ref' => ref,
                   'extractor_version' => VERSION, 'source_hashes' => {}}
    @definitions, @diagnostics, @resolved = {}, [], {}
  end

  def extract
    paths = Dir.glob(@root.join('app/models/**/*.rb').to_s).sort
    raise SchemaExtractionError, 'No Rails model files found' if paths.empty?
    paths.each do |file|
      raise SchemaExtractionError, 'Schema source symlinks are not supported' if File.symlink?(file)
      source = File.read(file, encoding: 'UTF-8')
      relative = Pathname.new(file).relative_path_from(@root).to_s.tr('\\', '/')
      @provenance['source_hashes'][relative] = Digest::SHA256.hexdigest(source)
      parsed = Prism.parse(source)
      raise SchemaExtractionError, "Ruby parse failed: #{relative}" unless parsed.success?
      discover(parsed.value.statements, '', relative)
    end
    check_inflections
    @definitions.each_key { |name| resolve(name) }
    collections = {}
    @resolved.each do |name, model|
      next unless model['document'] && !model['embedded']
      collection = model['collection'] || collection_name(name)
      validate_collection(collection)
      raise SchemaExtractionError, "Duplicate collection mapping: #{collection}" if collections.key?(collection)
      contract = model.reject { |key, _| %w[document embedded collection associations constants].include?(key) }
      contract['model'] = name
      contract['fields']['_id'] ||= {'types' => ['BSON::ObjectId']}
      contract['required'] |= ['_id']
      contract['references'] = references(model)
      attach_embedded(contract, model)
      contract['required'].map! { |field| model['associations'].fetch(field, {}).fetch('field', field) }
      contract['required'].uniq! # Preserve explicit presence validations only.
      contract['fields'] = contract['fields'].sort.to_h
      contract['required'].sort!
      collections[collection] = contract
    end
    add_reviewed_overrides(collections)
    raise SchemaExtractionError, 'No Mongoid collections extracted' if collections.empty?
    %w[config/initializers/inflections.rb docs/mongodb-collections.MD Gemfile.lock].each do |relative|
      file = @root.join(relative)
      next unless file.file?
      raise SchemaExtractionError, 'Schema source symlinks are not supported' if file.symlink?
      @provenance['source_hashes'][relative] = Digest::SHA256.file(file).hexdigest
    end
    @provenance['contract_hash'] = Digest::SHA256.hexdigest(JSON.generate(canonical(collections)))
    @provenance['build_time'] = Time.now.utc.iso8601(6)
    {'format_version' => 1, 'provenance' => @provenance,
     'collections' => collections.sort.to_h, 'diagnostics' => @diagnostics.sort_by { |d| [d['path'], d['line'], d['code']] }}
  end

  private

  def canonical(value)
    case value
    when Hash then value.sort.to_h.transform_values { |item| canonical(item) }
    when Array then value.map { |item| canonical(item) }
    else value
    end
  end

  def body(node)
    return [] unless node
    return node.body if node.is_a?(Prism::StatementsNode)
    [node]
  end

  def constant(node)
    case node
    when Prism::ConstantReadNode then node.name.to_s
    when Prism::ConstantPathNode then [node.parent && constant(node.parent), node.name.to_s].compact.join('::')
    else raise UnsupportedLiteral, 'not a constant'
    end
  end

  def discover(statements, scope, file)
    body(statements).each do |node|
      next unless node.is_a?(Prism::ClassNode) || node.is_a?(Prism::ModuleNode)
      name = constant(node.constant_path)
      name = "#{scope}::#{name}" unless scope.empty? || name.include?('::')
      definition = (@definitions[name] ||= {'nodes' => [], 'file' => file})
      definition['nodes'].concat(body(node.body))
      definition['parent'] = constant(node.superclass) if node.is_a?(Prism::ClassNode) && node.superclass
      discover(node.body, name, file)
    end
  end

  # This is a closed interpreter for literal AST nodes, not Ruby evaluation.
  def literal(node, constants = {}, seen = [])
    case node
    when Prism::SymbolNode, Prism::StringNode then node.unescaped
    when Prism::IntegerNode, Prism::FloatNode then node.value
    when Prism::TrueNode then true
    when Prism::FalseNode then false
    when Prism::NilNode then nil
    when Prism::ArrayNode then node.elements.map { |element| literal(element, constants, seen) }
    when Prism::HashNode, Prism::KeywordHashNode
      node.elements.to_h do |element|
        raise UnsupportedLiteral, 'hash expansion' unless element.is_a?(Prism::AssocNode)
        [literal(element.key, constants, seen).to_s, literal(element.value, constants, seen)]
      end
    when Prism::ParenthesesNode
      nodes = body(node.body)
      raise UnsupportedLiteral, 'multiple expressions' unless nodes.size == 1
      literal(nodes.first, constants, seen)
    when Prism::ConstantReadNode, Prism::ConstantPathNode
      name = constant(node)
      raise UnsupportedLiteral, 'unknown or recursive constant' if seen.include?(name) || !constants.key?(name)
      literal(constants[name], constants, seen + [name])
    when Prism::CallNode
      receiver = literal(node.receiver, constants, seen)
      args = node.arguments&.arguments || []
      return receiver if node.name == :freeze && args.empty?
      return receiver.keys if node.name == :keys && receiver.is_a?(Hash) && args.empty?
      return -receiver if node.name == :-@ && receiver.is_a?(Numeric) && args.empty?
      if node.name == :+ && receiver.is_a?(Array) && args.size == 1
        other = literal(args.first, constants, seen)
        return receiver + other if other.is_a?(Array)
      end
      raise UnsupportedLiteral, 'nonliteral method call'
    else raise UnsupportedLiteral, 'nonliteral expression'
    end
  end

  def arguments(node)
    node.arguments&.arguments || []
  end

  def option_nodes(node)
    hash = arguments(node).last
    return {} unless hash.is_a?(Prism::KeywordHashNode) || hash.is_a?(Prism::HashNode)
    hash.elements.to_h do |entry|
      raise UnsupportedLiteral, 'keyword expansion' unless entry.is_a?(Prism::AssocNode)
      [literal(entry.key), entry.value]
    end
  end

  def diagnostic(definition, node, code)
    @diagnostics << {'path' => definition['file'], 'line' => node.location.start_line, 'code' => code}
  end

  def resolve(name, visiting = [])
    return @resolved[name] if @resolved.key?(name)
    raise SchemaExtractionError, "Cyclic model/concern inclusion: #{name}" if visiting.include?(name)
    definition = @definitions.fetch(name)
    result = {'fields' => {}, 'required' => [], 'indexes' => [], 'references' => [], 'dynamic' => false,
              'associations' => {}, 'constants' => {}, 'document' => false, 'embedded' => false}
    parent = definition['parent']
    merge(result, resolve(parent, visiting + [name])) if parent && @definitions.key?(parent)
    nodes = declarations(definition['nodes'], definition)
    nodes.each do |node|
      result['constants'][node.name.to_s] = node.value if node.is_a?(Prism::ConstantWriteNode)
    end
    nodes.each do |node|
      next unless node.is_a?(Prism::CallNode) && node.receiver.nil?
      if node.name == :include
        arguments(node).each do |arg|
          included = constant(arg)
          if @definitions.key?(included)
            merge(result, resolve(included, visiting + [name]))
          elsif included == 'Mongoid::Document'
            result['document'] = true
          elsif TIMESTAMPS.key?(included)
            TIMESTAMPS[included].each { |field| result['fields'][field] = {'types' => ['Time']} }
          elsif included == 'Mongoid::Attributes::Dynamic'
            result['dynamic'] = true
          elsif included == 'Mongoid::Search'
            diagnostic(definition, node, 'search_generated_fields_not_expanded')
          end
        end
      end
    rescue UnsupportedLiteral
      raise SchemaExtractionError, "Unresolved include: #{definition['file']}:#{node.location.start_line}"
    end
    nodes.each do |node|
      next unless node.is_a?(Prism::CallNode) && node.receiver.nil?
      process_declaration(result, definition, node)
    end
    @resolved[name] = result
  end

  def declarations(nodes, definition)
    nodes.flat_map do |node|
      if node.is_a?(Prism::CallNode) && node.name == :included && node.block
        declarations(body(node.block.body), definition)
      elsif node.is_a?(Prism::DefNode) || node.is_a?(Prism::ClassNode) || node.is_a?(Prism::ModuleNode)
        []
      elsif node.is_a?(Prism::ConstantWriteNode) || node.is_a?(Prism::CallNode)
        if node.is_a?(Prism::CallNode) && %i[class_eval class_exec eval].include?(node.name)
          raise SchemaExtractionError, "Dynamic class evaluation: #{definition['file']}:#{node.location.start_line}"
        end
        if node.is_a?(Prism::CallNode) && node.block
          descendants = [node.block]
          until descendants.empty?
            candidate = descendants.pop
            if candidate.is_a?(Prism::CallNode) && STRUCTURAL.include?(candidate.name)
              raise SchemaExtractionError, "Dynamic schema block: #{definition['file']}:#{candidate.location.start_line}"
            end
            descendants.concat(candidate.compact_child_nodes) unless candidate.is_a?(Prism::DefNode)
          end
        end
        [node]
      else
        # Conditional or loop-created structural declarations cannot be inferred safely.
        descendants = [node]
        until descendants.empty?
          candidate = descendants.pop
          if candidate.is_a?(Prism::CallNode) && STRUCTURAL.include?(candidate.name)
            raise SchemaExtractionError, "Conditional schema declaration: #{definition['file']}:#{candidate.location.start_line}"
          end
          descendants.concat(candidate.compact_child_nodes) unless candidate.is_a?(Prism::DefNode)
        end
        []
      end
    end
  end

  def merge(target, source)
    %w[fields associations constants].each { |key| target[key].merge!(Marshal.load(Marshal.dump(source[key]))) }
    %w[required indexes].each { |key| target[key].concat(Marshal.load(Marshal.dump(source[key]))) }
    target['document'] ||= source['document']
    target['dynamic'] ||= source['dynamic']
    target['collection'] ||= source['collection']
  end

  def process_declaration(result, definition, node)
    constants = result['constants']
    case node.name
    when :field
      field = literal(arguments(node).first, constants)
      options = option_nodes(node)
      raise UnsupportedLiteral unless field.is_a?(String) && !field.empty?
      type = options['type'] ? constant(options['type']) : 'Object'
      types = type == 'FixTicketId' ? ['Integer', 'BSON::ObjectId'] : [type]
      raise SchemaExtractionError, "Unreviewed custom field type #{type}: #{definition['file']}" unless TYPES.include?(type) || type == 'FixTicketId'
      result['fields'][field] = {'types' => types}
      diagnostic(definition, node, 'untyped_field') unless options['type']
      diagnostic(definition, node, 'opaque_container') if %w[Hash Array Object].include?(type)
      if options['as']
        alias_name = literal(options['as'], constants)
        result['associations'][alias_name] = {'field' => field, 'alias' => true}
      end
    when :store_in
      options = option_nodes(node)
      result['collection'] = literal(options.fetch('collection'), constants) if options['collection']
      raise UnsupportedLiteral if options.key?('database') || options.key?('client')
    when :embedded_in
      result['embedded'] = true
    when :belongs_to, :has_and_belongs_to_many, :embeds_many, :embeds_one
      association = literal(arguments(node).first, constants)
      options = option_nodes(node)
      # Plural associations resolve to a singular class unless class_name overrides it.
      klass = options['class_name'] ? literal(options['class_name'], constants) : ActiveSupport::Inflector.camelize(ActiveSupport::Inflector.singularize(association))
      embedded = %i[embeds_many embeds_one].include?(node.name)
      field = if embedded
                options['store_as'] ? literal(options['store_as'], constants) : association
              elsif options['foreign_key']
                literal(options['foreign_key'], constants)
              else
                node.name == :has_and_belongs_to_many ? "#{ActiveSupport::Inflector.singularize(association)}_ids" : "#{association}_id"
              end
      array = %i[has_and_belongs_to_many embeds_many].include?(node.name)
      type = if array then 'Array'
             elsif embedded then 'Hash'
             elsif options['primary_key'] && literal(options['primary_key'], constants) != '_id' then 'Object'
             else 'BSON::ObjectId' end
      result['fields'][field] ||= {'types' => [type]}
      result['associations'][association] = {'field' => field, 'class' => klass, 'embedded' => embedded,
        'optional' => options['optional'] ? !!literal(options['optional'], constants) : false, 'array' => array,
        'target_field' => options['primary_key'] ? literal(options['primary_key'], constants) : '_id'}
    when :index
      keys = literal(arguments(node).first, constants)
      raise UnsupportedLiteral unless keys.is_a?(Hash) && keys.values.all? { |value| [1, -1, 'text', 'hashed', '2d', '2dsphere'].include?(value) }
      options = arguments(node).size > 1 ? literal(arguments(node)[1], constants) : {}
      result['indexes'] << {'keys' => keys, **options.select { |key, _| %w[unique sparse partial_filter_expression collation expire_after_seconds].include?(key) }}
    when :validates, :validates_presence_of, :validates_inclusion_of, :validates_numericality_of
      validation(result, definition, node)
    when :validate
      diagnostic(definition, node, 'custom_validation_not_executed')
    end
  rescue UnsupportedLiteral, KeyError
    if STRUCTURAL.include?(node.name)
      raise SchemaExtractionError, "Unresolved #{node.name}: #{definition['file']}:#{node.location.start_line}"
    end
    diagnostic(definition, node, 'unsupported_declaration')
  end

  def validation(result, definition, node)
    options = option_nodes(node)
    if (options.keys & %w[if unless on]).any?
      diagnostic(definition, node, 'conditional_validation_not_executed')
      return
    end
    fields = arguments(node).reject { |arg| arg.is_a?(Prism::KeywordHashNode) || arg.is_a?(Prism::HashNode) }.map { |arg| literal(arg, result['constants']) }
    fields.each do |field|
      if node.name == :validates_presence_of || options['presence'] && literal(options['presence'], result['constants']) == true
        result['required'] << field
      end
      storage = result['associations'].fetch(field, {}).fetch('field', field)
      next unless result['fields'].key?(storage)
      values = if node.name == :validates_inclusion_of && options['in']
                 literal(options['in'], result['constants'])
               elsif options['inclusion']
                 literal(options['inclusion'], result['constants']).fetch('in')
               end
      result['fields'][storage]['enum'] = values if values.is_a?(Array) && values.all? { |v| v.is_a?(String) || v.is_a?(Numeric) || [true, false, nil].include?(v) }
      # These constraints are metadata; the runtime only enforces supported ones.
      %w[length numericality].each do |key|
        result['fields'][storage][key] = literal(options[key], result['constants']) if options[key]
      end
      diagnostic(definition, node, 'format_validation_not_executed') if options['format']
    end
  end

  def collection_name(name)
    ActiveSupport::Inflector.tableize(name).tr('/', '_')
  end

  def validate_collection(name)
    raise SchemaExtractionError, "Unsafe source collection name: #{name.inspect}" unless name.is_a?(String) && name.match?(/\A[a-z][a-z0-9_]*\z/) && !name.start_with?('ledger_', 'system.')
  end

  def references(model)
    model['associations'].values.filter_map do |association|
      next if association['embedded'] || association['alias']
      target = @resolved[association['class']]
      {'field' => association['field'], 'collection' => target&.fetch('collection', nil) || collection_name(association['class']),
       'optional' => association['optional'], 'array' => association['array'], 'target_field' => association['target_field']}
    end
  end

  def attach_embedded(contract, model)
    model['associations'].each_value do |association|
      next unless association['embedded']
      target = @resolved[association['class']]
      raise SchemaExtractionError, "Missing embedded model: #{association['class']}" unless target && target['embedded']
      contract['fields'][association['field']]['embedded'] = {'fields' => target['fields'], 'required' => target['required'], 'dynamic' => target['dynamic']}
    end
  end

  def check_inflections
    file = @root.join('config/initializers/inflections.rb')
    return unless file.file?
    source = file.read
    @provenance['source_hashes']['config/initializers/inflections.rb'] = Digest::SHA256.hexdigest(source)
    parsed = Prism.parse(source)
    raise SchemaExtractionError, 'Invalid inflections syntax' unless parsed.success?
    raise SchemaExtractionError, 'Custom inflections require a reviewed extractor adapter' unless body(parsed.value.statements).empty?
  end

  def add_reviewed_overrides(collections)
    collections['checkins'] = {'model' => nil, 'fields' => {'_id' => {'types' => ['BSON::ObjectId']},
      'uid' => {'types' => ['String']}, 'time' => {'types' => ['String', 'Integer', 'Time']},
      'timeOf' => {'types' => ['Integer', 'Float', 'Time', 'String']}},
      'required' => ['_id'], 'indexes' => [], 'references' => [], 'dynamic' => true, 'origin' => 'reviewed_direct_collection'}
    collections['notes'] = {'model' => nil, 'fields' => {'_id' => {'types' => ['BSON::ObjectId']},
      'member_id' => {'types' => ['BSON::ObjectId', 'String']}}, 'required' => ['_id'],
      'indexes' => [{'keys' => {'member_id' => 1}}], 'references' => [], 'dynamic' => true,
      'origin' => 'reviewed_direct_collection', 'structure_only' => true}
    %w[fix_ticket_reveals registration_tokens braintree__notifications].each do |name|
      collections[name]['structure_only'] = true if collections[name]
    end
    # Counter uses a named string primary key; legacy ids are not ObjectIds.
    collections['counter']['fields']['_id'] = {'types' => ['String']} if collections['counter']
  end
end

if $PROGRAM_NAME == __FILE__
  options = {'repository' => 'https://github.com/ManchesterMakerspace/makerspace-rails-2026.git', 'ref' => 'refs/heads/master'}
  OptionParser.new do |parser|
    parser.on('--root PATH') { |value| options['root'] = value }
    parser.on('--commit SHA') { |value| options['commit'] = value }
    parser.on('--repository URL') { |value| options['repository'] = value }
    parser.on('--ref REF') { |value| options['ref'] = value }
    parser.on('--output PATH') { |value| options['output'] = value }
  end.parse!
  begin
    raise SchemaExtractionError, 'Supply --root and a resolved --commit SHA' unless options['root'] && options['commit']&.match?(/\A[0-9a-f]{40,64}\z/)
    result = RailsSchemaExtractor.new(options['root'], commit: options['commit'], repository: options['repository'], ref: options['ref']).extract
    text = JSON.pretty_generate(result) + "\n"
    options['output'] ? File.write(options['output'], text) : puts(text)
  rescue SchemaExtractionError, UnsupportedLiteral => error
    warn "Schema extraction failed: #{error.message}"
    exit 1
  end
end
