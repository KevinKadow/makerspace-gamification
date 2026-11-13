"""Deployment switches for additive interactive read optimizations."""
import os


def optimized_reads():
    return os.environ.get("LEDGER_OPTIMIZED_READS", "true").lower() in ("true", "1", "yes")

