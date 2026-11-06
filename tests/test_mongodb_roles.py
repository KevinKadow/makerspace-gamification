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


def test_role_grants_match_actual_collections_deletes_and_indexes():
    trees = [ast.parse(path.read_text(encoding="utf-8")) for path in (ROOT / "ledger").glob("*.py")]
    collections, sources, deletes, indexed = set(), set(), set(), set()
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"ledger_[a-z_]+", node.value):
                collections.add(node.value)
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "rows" and node.args:
                # Includes the shops/tools conditional dropdown expression.
                sources.update(collection_literals(node.args[0]))
            if node.func.attr == "delete" and node.args and isinstance(node.args[0], ast.Constant):
                deletes.add(node.args[0].value)
            if node.func.attr == "create_index":
                indexed.add(node.func.value.attr)
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
