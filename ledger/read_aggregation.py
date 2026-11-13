"""Small aggregation evaluator for the transactional memory test adapter.

Only application-used stages are supported; unsupported stages fail explicitly.
Production pipelines always execute in MongoDB.
"""
from copy import deepcopy
import json

from .storage import field, matches, project, sort_rows


def expression(value, doc):
    if isinstance(value, str) and value.startswith("$"):
        return field(doc, value[1:])
    if isinstance(value, list):
        return [expression(v, doc) for v in value]
    if not isinstance(value, dict):
        return value
    if len(value) != 1 or not next(iter(value), "").startswith("$"):
        return {k: expression(v, doc) for k, v in value.items()}
    op, args = next(iter(value.items()))
    values = expression(args, doc)
    if op == "$literal":
        return args
    if op == "$ifNull":
        return next((v for v in values if v is not None), None)
    if op == "$cond":
        return expression(args[1] if expression(args[0], doc) else args[2], doc)
    if op == "$eq":
        return values[0] == values[1]
    if op == "$ne":
        return values[0] != values[1]
    if op == "$in":
        return values[0] in values[1]
    if op == "$and":
        return all(values)
    if op == "$or":
        return any(values)
    if op == "$toString":
        return str(values) if values is not None else None
    if op == "$size":
        return len(values)
    raise ValueError("Unsupported memory aggregation expression: " + op)


def run(store, rows, pipeline):
    for stage in pipeline:
        op, value = next(iter(stage.items()))
        if op == "$match":
            rows = [r for r in rows if matches(r, value)]
        elif op == "$sort":
            sort_rows(rows, list(value.items()))
        elif op == "$limit":
            rows = rows[:value]
        elif op == "$skip":
            rows = rows[value:]
        elif op == "$count":
            rows = [{value: len(rows)}] if rows else []
        elif op == "$group":
            groups = {}
            for row in rows:
                identity = expression(value["_id"], row)
                key = json.dumps(identity, sort_keys=True, default=str)
                result = groups.setdefault(key, {"_id": identity})
                for name, accumulator in value.items():
                    if name == "_id":
                        continue
                    method, expr = next(iter(accumulator.items()))
                    val = expression(expr, row)
                    if method == "$sum":
                        result[name] = result.get(name, 0) + (val if isinstance(val, (int, float)) else 0)
                    elif method == "$first":
                        result.setdefault(name, deepcopy(val))
                    else:
                        raise ValueError("Unsupported memory accumulator: " + method)
            rows = list(groups.values())
        elif op == "$facet":
            rows = [{key: run(store, deepcopy(rows), stages) for key, stages in value.items()}]
        elif op == "$lookup":
            for row in rows:
                row[value["as"]] = store.select(value["from"], {
                    value["foreignField"]: field(row, value["localField"])})
        elif op == "$unwind":
            path = value[1:] if isinstance(value, str) else value["path"][1:]
            expanded = []
            for row in rows:
                for element in field(row, path, []) or []:
                    new = deepcopy(row)
                    target = new
                    parts = path.split(".")
                    for part in parts[:-1]:
                        target = target[part]
                    target[parts[-1]] = element
                    expanded.append(new)
            rows = expanded
        elif op == "$project":
            if all(v in (0, 1) for v in value.values()):
                rows = [project(r, value) for r in rows]
            else:
                computed = []
                for row in rows:
                    result = project(row, {k: v for k, v in value.items() if v in (0, 1)})
                    for key, expr in value.items():
                        if expr not in (0, 1):
                            result[key] = expression(expr, row)
                    computed.append(result)
                rows = computed
        else:
            raise ValueError("Unsupported memory aggregation stage: " + op)
    return rows


def aggregate(store, collection, pipeline):
    return run(store, store.select(collection), pipeline)
