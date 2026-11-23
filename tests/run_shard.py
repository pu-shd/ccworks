"""Run a deterministic module-level partition of the complete unittest suite."""

import argparse
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

# Scheduling hints from CI timings, including fixture setup. Unknown modules
# are always discovered and included; these hints never select test coverage.
MODULE_SECONDS = {
    "tests.test_allocations_crud": 290,
    "tests.test_receipt_attach": 270,
    "tests.test_row_indexing": 190,
    "tests.test_justification": 70,
    "tests.test_bulk_allocations": 65,
    "tests.test_save_dialogs": 65,
    "tests.test_submission_resilience": 45,
    "tests.test_allocation_resilience": 40,
    "tests.test_type_updates": 35,
    "tests.test_submit_confirmation": 20,
    "tests.test_cli_surface": 10,
    "tests.test_session_handoff_browser": 10,
}


def discover_modules(directory: Path) -> list[str]:
    return sorted(f"tests.{path.stem}" for path in directory.glob("test*.py"))


def partition_modules(modules: list[str], count: int) -> list[list[str]]:
    if count < 1:
        raise ValueError("Shard count must be positive")
    if count > len(modules):
        raise ValueError("Shard count exceeds the available test modules")
    if len(modules) != len(set(modules)):
        raise ValueError("Duplicate test modules")
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0] * count
    for module in sorted(
        modules, key=lambda name: (-MODULE_SECONDS.get(name, 1), name)
    ):
        index = min(range(count), key=lambda i: (loads[i], i))
        shards[index].append(module)
        loads[index] += MODULE_SECONDS.get(module, 1)
    return [sorted(shard) for shard in shards]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=int, help="Zero-based shard")
    parser.add_argument("--count", required=True, type=int, help="Total shard count")
    parser.add_argument(
        "--list", action="store_true", help="Show modules without running"
    )
    args = parser.parse_args(argv)
    if args.count < 1 or not 0 <= args.index < args.count:
        parser.error("Require count > 0 and 0 <= index < count")
    modules = discover_modules(ROOT / "tests")
    if not modules:
        print("FAIL: no test modules discovered", file=sys.stderr)
        return 2
    if args.count > len(modules):
        parser.error("Shard count exceeds the available test modules")
    selected = partition_modules(modules, args.count)[args.index]
    if not selected:
        print("FAIL: empty test shard", file=sys.stderr)
        return 2
    print(f"Shard {args.index + 1}/{args.count}: {len(selected)} modules", flush=True)
    for module in selected:
        print(f"  {module}", flush=True)
    if args.list:
        return 0
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(selected)
    if not suite.countTestCases():
        print("FAIL: shard contains no test cases", file=sys.stderr)
        return 2
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.wasSuccessful() and not loader.errors
    print(
        f"{'PASS' if passed else 'FAIL'}: shard {args.index + 1}/{args.count}, "
        f"{result.testsRun} tests, {len(result.skipped)} skips, "
        f"{time.monotonic() - started:.1f}s",
        flush=True,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
