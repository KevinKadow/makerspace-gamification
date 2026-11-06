"""Execute the JS examples without contacting Mongo; compare grants to call sites."""
import ast
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="Node.js is needed to validate the JavaScript role examples")


def definition(format, kind):
    result = subprocess.run([NODE, "examples/mongodb-roles.js", format, kind, "source_db", "game_db"],
                            cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def collection_literals(expression):
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return {expression.value}
    if isinstance(expression, ast.IfExp):
        return collection_literals(expression.body) | collection_literals(expression.orelse)
    return set()


def owned_collection_literals(tree):
    """Follow static collection arguments, not similarly named roles/kinds/callbacks."""
    bindings = {}
    def bind(name, value):
        bindings.setdefault(name, []).append(value)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bind(target.id, node.value)
        if isinstance(node, ast.For):
            if isinstance(node.target, ast.Name):
                bind(node.target.id, node.iter)
            elif isinstance(node.target, (ast.Tuple, ast.List)) and isinstance(node.iter, (ast.Tuple, ast.List)):
                for row in node.iter.elts:
                    if isinstance(row, (ast.Tuple, ast.List)):
                        for target, value in zip(node.target.elts, row.elts):
                            if isinstance(target, ast.Name):
                                bind(target.id, value)
    def resolve(expression, seen=frozenset()):
        if isinstance(expression, ast.Name) and expression.id not in seen:
            return set().union(*(resolve(value, seen | {expression.id}) for value in bindings.get(expression.id, [])))
        if isinstance(expression, (ast.Tuple, ast.List, ast.Set)):
            return set().union(*(resolve(value, seen) for value in expression.elts))
        if isinstance(expression, ast.IfExp):
            return resolve(expression.body, seen) | resolve(expression.orelse, seen)
        return collection_literals(expression)
    result = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ('get', 'put', 'put_many', 'select', 'delete', 'count', 'exists', 'aggregate') and node.args):
            receiver = node.func.value
            # Dict.get/string.count also accept role-like text. Owned reads use
            # the store attribute or the store transaction aliases.
            if node.func.attr in ('get', 'count', 'exists', 'aggregate') and not (
                    isinstance(receiver, ast.Name) and receiver.id in ('store', 's', 'tx', 'self')
                    or isinstance(receiver, ast.Attribute) and receiver.attr == 'store'):
                continue
            result.update(value for value in resolve(node.args[0]) if re.fullmatch(r'ledger_[a-z_]+', value))
    return result


def joined_collection_literals(tree):
    """Count only collection inputs of aggregation joins, never role/output names."""
    collections = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant):
                continue
            if key.value == "$unionWith" and isinstance(value, ast.Constant) and isinstance(value.value, str):
                collections.add(value.value)
            elif key.value in ("$lookup", "$graphLookup", "$unionWith") and isinstance(value, ast.Dict):
                name = "coll" if key.value == "$unionWith" else "from"
                for nested_key, nested_value in zip(value.keys, value.values):
                    if isinstance(nested_key, ast.Constant) and nested_key.value == name:
                        collections.update(collection_literals(nested_value))
    return collections


def test_collection_inventory_ignores_role_kind_and_callback_names_but_follows_collection_lists():
    tree = ast.parse('''
roles = {"ledger_quest_author"}
kinds = ("ledger_quest",)
views.modal("ledger_quest_review", "Review", [])
names = ("ledger_quests", "ledger_new_collection")
for collection in names:
    store.put(collection, {})
store.get("ledger_quest", "real-collection-with-kind-name")
''')
    assert owned_collection_literals(tree) == {'ledger_quests', 'ledger_new_collection', 'ledger_quest'}


def test_inventory_covers_read_helpers_and_nested_join_inputs_only():
    tree = ast.parse('''
store.count("ledger_awards")
store.exists("ledger_catalog")
store.put_many("ledger_evidence", [])
text.count("ledger_fake_role")
labels.exists("ledger_fake_kind")
store.aggregate("ledger_relationships", [
    {"$lookup": {"from": "ledger_quests", "as": "ledger_quest_author",
                  "pipeline": [{"$lookup": {"from": "members", "as": "member"}}]}},
    {"$graphLookup": {"from": "tools", "as": "ancestors"}},
    {"$unionWith": {"coll": "ledger_evidence"}}, {"$unionWith": "shops"},
    {"$lookup": {"pipeline": [{"$documents": [{"from": "not_a_collection"}]}], "as": "rows"}}
])
role = {"from": "ledger_quest_author"}
''')
    assert owned_collection_literals(tree) == {'ledger_awards', 'ledger_catalog', 'ledger_relationships', 'ledger_evidence'}
    assert joined_collection_literals(tree) == {'ledger_quests', 'members', 'tools', 'ledger_evidence', 'shops'}


def test_role_grants_match_actual_collections_deletes_and_indexes():
    trees = [ast.parse(path.read_text(encoding="utf-8")) for path in (ROOT / "ledger").glob("*.py")]
    collections, sources, deletes, indexed = set(), set(), set(), set()
    for tree in trees:
        collections.update(owned_collection_literals(tree))
        joins = joined_collection_literals(tree)
        collections.update(name for name in joins if name.startswith("ledger_"))
        sources.update(name for name in joins if not name.startswith("ledger_"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in ("rows", "bounded", "by_ids", "_aggregate", "_projected_rows", "catalog_query") and node.args:
                # Includes the shops/tools conditional dropdown expression.
                sources.update(collection_literals(node.args[0]))
            if node.func.attr == "delete" and node.args and isinstance(node.args[0], ast.Constant):
                deletes.add(node.args[0].value)
            if node.func.attr == "create_index":
                receiver = node.func.value
                if isinstance(receiver, ast.Attribute):
                    indexed.add(receiver.attr)
                elif isinstance(receiver, ast.Subscript) and isinstance(receiver.slice, ast.Name):
                    # Index initialization iterates a literal list of owned collections.
                    loops = [loop for loop in ast.walk(tree) if isinstance(loop, ast.For)
                             and isinstance(loop.target, ast.Name) and loop.target.id == receiver.slice.id
                             and any(child is node for child in ast.walk(loop))]
                    assert loops and all(isinstance(loop.iter, (ast.Tuple, ast.List)) for loop in loops)
                    indexed.update(name for loop in loops for value in loop.iter.elts for name in collection_literals(value))
                else:
                    raise AssertionError('Unresolved index collection')
    from ledger.query_tools import PROJECTIONS
    sources.update(PROJECTIONS)
    read_role = definition("mongosh", "source")
    write_role = definition("mongosh", "ledger")
    assert {p["resource"]["collection"] for p in read_role["privileges"]} == sources
    assert all(p["actions"] == ["find"] and p["resource"]["db"] == "source_db" for p in read_role["privileges"])
    assert {p["resource"]["collection"] for p in write_role["privileges"]} == collections
    for p in write_role["privileges"]:
        collection = p["resource"]["collection"]
        expected = {"find", "insert", "update"}
        if collection in deletes:
            expected.add("remove")
        if collection in indexed:
            expected.add("createIndex")
        assert set(p["actions"]) == expected
        assert p["resource"]["db"] == "game_db"
    assert read_role["roles"] == write_role["roles"] == []


def test_atlas_payload_uses_correct_schema_and_exact_union_without_wildcards():
    mongo = definition("mongosh", "gamification")
    atlas = definition("atlas", "gamification")
    expected = {(p["resource"]["db"], p["resource"]["collection"], a) for p in mongo["privileges"] for a in p["actions"]}
    mapping = {"FIND": "find", "INSERT": "insert", "UPDATE": "update", "REMOVE": "remove", "CREATE_INDEX": "createIndex"}
    actual = set()
    for action in atlas["actions"]:
        for r in action["resources"]:
            assert set(r) == {"cluster", "db", "collection"}
            assert r["cluster"] is False and r["db"] and r["collection"] and "*" not in r["collection"]
            actual.add((r["db"], r["collection"], mapping[action["action"]]))
    assert actual == expected
    assert atlas["roleName"] == "gamification" and atlas["inheritedRoles"] == []


def test_mongosh_example_creates_only_the_two_scoped_roles_in_admin():
    # VM mock provides only load/getSiblingDB/createRole: no network/real DB user.
    script = """
      const fs = require('node:fs'), vm = require('node:vm');
      const calls = [];
      const context = vm.createContext({db: {getSiblingDB(name) {
        if (name !== 'admin') throw new Error('Unexpected role database');
        return {createRole(role) { calls.push(role); }};
      }}});
      context.load = path => vm.runInContext(fs.readFileSync(path, 'utf8'), context);
      context.load('examples/create-gamification-role.mongosh.js');
      console.log(JSON.stringify(calls));
    """
    result = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)
    roles = json.loads(result.stdout)
    assert [r["role"] for r in roles] == ["gamification_source_reader", "gamification_ledger_writer"]
    assert all(p["actions"] == ["find"] for p in roles[0]["privileges"])
    assert all(p["resource"]["collection"].startswith("ledger_") for p in roles[1]["privileges"])


def test_ticket_note_writer_is_separate_and_limited_to_revision_event_path():
    role = definition("mongosh", "ticket_note_writer")
    grants = {(p["resource"]["collection"], tuple(p["actions"])) for p in role["privileges"]}
    assert grants == {("fix_tickets", ("find", "update")), ("fix_ticket_events", ("find", "insert"))}
