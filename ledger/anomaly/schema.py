"""BSON checks and reviewed schema generations; source code never grants data access."""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re

from bson import ObjectId, Decimal128, Binary

from .state import stamp


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(value):
    return sha256(canonical(value).encode()).hexdigest()


def load_manifest(path):
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("format_version") != 1 or not isinstance(manifest.get("collections"), dict) or not manifest["collections"]:
        raise ValueError("Invalid schema manifest")
    for name, schema in manifest["collections"].items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) or name.startswith(("ledger_", "system.")):
            raise ValueError("Schema must enumerate source collections only")
        if not isinstance(schema.get("fields"), dict):
            raise ValueError("Invalid schema fields")
    return manifest


def kind(value):
    if value is None: return "NilClass"
    if type(value) is bool: return "Boolean"
    if isinstance(value, ObjectId): return "BSON::ObjectId"
    if isinstance(value, datetime): return "Time"
    if isinstance(value, date): return "Date"
    if isinstance(value, (Decimal, Decimal128)): return "BigDecimal"
    if isinstance(value, int): return "Integer"  # Includes BSON Int64; bool was handled above.
    if type(value) is float: return "Float"
    if isinstance(value, str): return "String"
    if isinstance(value, dict): return "Hash"
    if isinstance(value, (list, tuple)): return "Array"
    if isinstance(value, (bytes, Binary)): return "Binary"
    return "Object"


def accepts(value, types):
    aliases = {"ObjectId": "BSON::ObjectId", "DateTime": "Time", "Symbol": "String", "Mongoid::Boolean": "Boolean",
               "TrueClass": "Boolean", "FalseClass": "Boolean", "BSON::Decimal128": "BigDecimal"}
    allowed = {aliases.get(t, t) for t in types}
    if "FixTicketId" in allowed:
        allowed.update({"Integer", "BSON::ObjectId"})
    if not allowed or allowed & {"Object", "untyped", "Any"} or value is None:
        return True
    actual = kind(value)
    return (actual in allowed or ("Date" in allowed and actual == "Time")
            or ("Float" in allowed and actual == "Integer")
            or ("Numeric" in allowed and actual in {"Integer", "Float", "BigDecimal"}))


def validate_document(schema, document, prefix=""):
    """No field values in findings; unknown names are hashed rather than logged."""
    findings = []
    for name in schema.get("required", []):
        if document.get(name) is None or document.get(name) == "":
            findings.append({"rule": "required", "field": prefix + name, "severity": "error"})
    for name, value in document.items():
        field = schema["fields"].get(name)
        if field is None:
            if name != "_id" and not schema.get("dynamic"):
                findings.append({"rule": "unknown_field", "field": prefix + "hash:" + fingerprint(name)[:16], "severity": "info"})
            continue
        if not accepts(value, field.get("types", [])):
            findings.append({"rule": "type", "field": prefix + name, "observed_type": kind(value), "severity": "error"})
        if value is not None and "enum" in field and value not in field["enum"]:
            findings.append({"rule": "enum", "field": prefix + name, "severity": "error"})
        embedded = field.get("embedded")
        if embedded and isinstance(value, dict):
            findings.extend(validate_document(embedded, value, prefix + name + "."))
        elif embedded and isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, dict):
                    findings.extend(validate_document(embedded, item, prefix + name + "[]."))
                else:
                    findings.append({"rule": "type", "field": prefix + name + "[]", "observed_type": kind(item), "severity": "error"})
    # Findings describe schema paths, not array offsets or repeated private values.
    return list({canonical(finding): finding for finding in findings}.values())


def compatibility(previous, candidate):
    """Return incompatible collection names. Unknown validations stay diagnostic-only."""
    incompatible = {}
    for name, old in previous.items():
        new = candidate.get(name)
        reasons = []
        if new is None:
            reasons.append("collection_removed")
        else:
            if old.get("dynamic") and not new.get("dynamic"):
                reasons.append("dynamic_fields_disabled")
            if set(new.get("required", [])) - set(old.get("required", [])):
                reasons.append("required_added")
            for field, spec in old["fields"].items():
                latest = new["fields"].get(field)
                if latest is None:
                    reasons.append("field_removed:" + field)
                elif latest.get("types") and (not spec.get("types") or not set(spec["types"]) <= set(latest["types"])):
                    reasons.append("type_changed:" + field)
                elif "enum" in latest and ("enum" not in spec or not {canonical(v) for v in spec["enum"]} <= {canonical(v) for v in latest["enum"]}):
                    reasons.append("enum_narrowed:" + field)
                if latest and latest.get("embedded"):
                    if not spec.get("embedded") or compatibility({field: spec["embedded"]}, {field: latest["embedded"]}):
                        reasons.append("embedded_changed:" + field)
            if old.get("references", []) != new.get("references", []):
                reasons.append("references_changed")
            if any(index.get("unique") and index not in old.get("indexes", []) for index in new.get("indexes", [])):
                reasons.append("unique_index_changed")
        if reasons:
            incompatible[name] = reasons
    return incompatible


def compatible_contract(previous, candidate):
    """Activate independent additions while retaining only constraints needing review."""
    if candidate is None:
        return deepcopy(previous)
    effective = deepcopy(candidate)
    effective["required"] = [field for field in candidate.get("required", []) if field in previous.get("required", [])]
    if previous.get("dynamic") and not candidate.get("dynamic"):
        effective["dynamic"] = True
    for field, old in previous["fields"].items():
        new = candidate["fields"].get(field)
        if new is None or new.get("types") and (not old.get("types") or not set(old["types"]) <= set(new["types"])):
            effective["fields"][field] = deepcopy(old)
            continue
        if "enum" in new and ("enum" not in old or not {canonical(v) for v in old["enum"]} <= {canonical(v) for v in new["enum"]}):
            if "enum" in old:
                effective["fields"][field]["enum"] = deepcopy(old["enum"])
            else:
                effective["fields"][field].pop("enum", None)
        if new.get("embedded"):
            if old.get("embedded"):
                effective["fields"][field]["embedded"] = compatible_contract(old["embedded"], new["embedded"])
            else:
                effective["fields"][field].pop("embedded", None)
    if previous.get("references", []) != candidate.get("references", []):
        effective["references"] = deepcopy(previous.get("references", []))
    previous_indexes = previous.get("indexes", [])
    effective["indexes"] = []
    for index in candidate.get("indexes", []):
        if not index.get("unique") or index in previous_indexes:
            effective["indexes"].append(deepcopy(index))
        else:
            effective["indexes"].extend(deepcopy(old) for old in previous_indexes if old.get("unique") and old.get("keys") == index.get("keys"))
    return effective


def required_paths(schema, prefix=""):
    paths = {prefix + name for name in schema.get("required", [])}
    for name, field in schema.get("fields", {}).items():
        if field.get("embedded"):
            suffix = "[]." if "Array" in field.get("types", []) else "."
            paths.update(required_paths(field["embedded"], prefix + name + suffix))
    return paths


class SchemaRegistry:
    def __init__(self, state, manifest):
        self.state, self.manifest = state, manifest
        identity = deepcopy(manifest)
        identity.setdefault("provenance", {}).pop("build_time", None)
        self.version = fingerprint(identity)

    def provenance(self):
        return {key: value for key, value in self.manifest.get("provenance", {}).items()
                if key in {"rails_commit", "contract_hash", "extractor_version", "build_time"} and isinstance(value, str)}

    def required_since(self, collection):
        return self.state.get("schemas", "active", {}).get("required_since", {}).get(collection, {})

    def health(self, pending):
        self.state.put("health", "schema", {"status": "review" if pending else "ready", "updated_at": stamp(),
            "version": self.version, "pending_collections": sorted(pending), **self.provenance()})

    def install(self):
        with self.state.atomic():
            prior = self.state.get("schemas", "active")
            candidate = self.manifest["collections"]
            blocked = compatibility(prior["collections"], candidate) if prior else {}
            active = deepcopy(candidate)
            for name in blocked:
                active[name] = compatible_contract(prior["collections"][name], candidate.get(name))
            current = {"version": self.version, "collections": active, "provenance": self.manifest.get("provenance", {}),
                       "pending": blocked, "candidate": candidate, "required_since": (prior or {}).get("required_since", {})}
            self.state.put("schema_history", self.version, current)
            self.state.put("schemas", "active", current)
            self.health(blocked)
        return current

    def approve(self, version, actor):
        with self.state.atomic():
            current = self.state.get("schemas", "active")
            if not current or version != current["version"]:
                raise ValueError("Approve the exact current schema revision")
            accepted_at = stamp()
            applicability = current.setdefault("required_since", {})
            for collection, candidate in current["candidate"].items():
                old = current["collections"].get(collection)
                if old:
                    newly_required = required_paths(candidate) - required_paths(old)
                    applicability.setdefault(collection, {}).update({field: accepted_at for field in newly_required})
            current.update(collections=current["candidate"], pending={}, approved_by=actor)
            self.state.put("schemas", "active", current)
            self.state.put("schema_history", version, current)
            self.health({})

    def contracts(self):
        return self.state.get("schemas", "active", {}).get("collections", self.manifest["collections"])
