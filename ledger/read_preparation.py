"""Explicit additive preparation and read-only shadow comparisons."""
from contextlib import contextmanager
import os


@contextmanager
def _mode(enabled):
    previous = os.environ.get("LEDGER_OPTIMIZED_READS")
    os.environ["LEDGER_OPTIMIZED_READS"] = "true" if enabled else "false"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("LEDGER_OPTIMIZED_READS", None)
        else:
            os.environ["LEDGER_OPTIMIZED_READS"] = previous


def prepare(ledger):
    from .catalog_cache import refresh
    from .context_reads import backfill_context_order
    from .quest_discovery import backfill_quest_heads
    getattr(ledger.store, "indexes", lambda: None)()
    result = {"quest_heads": backfill_quest_heads(ledger.store),
              "context_order": backfill_context_order(ledger.store)}
    # Preparation must build display data while ordinary workers still use the
    # operator's disabled rollout switch. No Slack/Docs/inference is involved.
    with _mode(True):
        manifest = refresh(ledger)
    result["catalog"] = {"available": bool(manifest), "shops": (manifest or {}).get("shop_count", 0),
                         "tools": (manifest or {}).get("tool_count", 0)}
    return result


def verify(ledger, sample_size=25):
    from pymongo.errors import PyMongoError
    from .domain import Denied
    from .metrics import snapshot
    from .query_tools import QueryTools
    from .quests import Quests
    from .read_metrics import READ_METRICS
    from .skills import highest_skill, skill_summary
    counts, mismatches, skipped, measurements, expected = 0, [], 0, {}, []
    def compare(label, fn):
        nonlocal counts, skipped
        outputs = []
        for enabled in (False, True):
            READ_METRICS.snapshot(reset=True)
            try:
                with _mode(enabled):
                    value = fn()
            except Denied:
                value = ("denied",)
            except ValueError as error:
                if not enabled and str(error) in ("Enabled catalog exceeds the supported bound", "Tool catalog exceeds the supported bound"):
                    value = ("legacy-catalog-bound",)
                else:
                    value = ("error", type(error).__name__)
            except (PyMongoError, OSError, TimeoutError) as error:
                value = ("error", type(error).__name__)
            measurements[label + (":optimized" if enabled else ":legacy")] = READ_METRICS.snapshot(reset=True)
            if isinstance(value, dict):
                value = {k: v for k, v in value.items() if k != "retrieved_at"}
            outputs.append(value)
        if outputs == [("denied",), ("denied",)]:
            skipped += 1
            return
        counts += 1
        if outputs[0] == ("legacy-catalog-bound",) and isinstance(outputs[1], dict) and outputs[1].get("status") == "ok":
            # Removing this legacy cap is an explicitly intended behavior change.
            expected.append(label + ":legacy-catalog-bound")
        elif any(isinstance(v, tuple) and v and v[0] == "error" for v in outputs):
            mismatches.append(label + ":error")
        elif any(isinstance(v, dict) and v.get("status") == "unavailable" for v in outputs):
            mismatches.append(label + ":unavailable")
        elif outputs[0] != outputs[1]:
            # Labels contain sample positions only, never member IDs or bodies.
            mismatches.append(label)
    compare("metrics", lambda: snapshot(ledger.store))
    people = ledger.store.select("ledger_participants", {"opted_in": True},
               projection={"member_id": 1}, sort=[("_id", 1)], limit=sample_size, max_time_ms=2000)
    for index, row in enumerate(people):
        member = row["member_id"]
        compare(f"sample-{index}:highest-skill", lambda: highest_skill(ledger.sources, member))
        compare(f"sample-{index}:skills", lambda: skill_summary(ledger, member))
        compare(f"sample-{index}:quests", lambda: Quests(ledger).options(member))
        for collection in ("shops", "tools", "tool_checkouts", "volunteer_tasks", "volunteer_events"):
            compare(f"sample-{index}:{collection}", lambda: QueryTools(ledger, member).query({"collection": collection}))
    return {"cases": counts, "mismatches": mismatches, "skipped": skipped,
            "expected_differences": expected, "commands": measurements}
