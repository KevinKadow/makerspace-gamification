"""One-shot operator CLI: python -m ledger.generate_quest --help."""
import argparse
import json
import os
import sys
from uuid import uuid4

from .cli import dependencies
from .quest_generation import QuestGenerationError, QuestGenerator


def parser():
    result = argparse.ArgumentParser(description="The Ledger authors one quest and submits it for human review.")
    result.add_argument("--type", choices=("individual", "cooperative"), default="individual", dest="quest_type")
    result.add_argument("--rank", type=int, help="Enabled numeric rank slot; omitted selects a weighted random rank.")
    result.add_argument("--dry-run", action="store_true", help="Generate and validate without database writes or Slack posts.")
    result.add_argument("--seed", type=int, help="Reproduce rank/example sampling, not model output.")
    result.add_argument("--request-id", help="Resume with the same type, rank and seed; omitted creates a UUID.")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    request_id = args.request_id or str(uuid4())
    print(json.dumps({"request_id": request_id, "stage": "starting"}), flush=True)
    try:
        ledger, composer, slack = dependencies()
        ledger.store.ready()
        ledger.sources.ready()
        generator = QuestGenerator(ledger, composer.api, composer.matrix, slack,
            os.environ.get("LEDGER_QUEST_REVIEW_CHANNEL_ID", ""), int(os.environ.get("LEDGER_QUEST_CONTEXT_LIMIT", "8192")))
        result = generator.run(args.quest_type, args.rank, args.seed, request_id, args.dry_run)
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return 0
    except Exception as exc:
        guidance = str(exc) if isinstance(exc, QuestGenerationError) else (
            "Check source, Slack and inference availability; retry with this request ID and original options.")
        print(json.dumps({"request_id": request_id, "status": "failed", "error": type(exc).__name__,
            "guidance": guidance}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
