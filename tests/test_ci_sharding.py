"""Coverage, ordering, and failure contracts for parallel container tests."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

from tests import run_shard

ROOT = Path(__file__).resolve().parents[1]


def test_ids(suite):
    result = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            result.extend(test_ids(item))
        else:
            result.append(item.id())
    return result


class TestCISharding(unittest.TestCase):
    def test_every_discovered_case_runs_once_and_module_order_is_preserved(self):
        modules = run_shard.discover_modules(ROOT / "tests")
        shards = run_shard.partition_modules(modules, 4)
        flattened = [name for shard in shards for name in shard]
        self.assertCountEqual(modules, flattened)
        self.assertEqual(len(flattened), len(set(flattened)))
        expected_loader = unittest.TestLoader()
        expected = expected_loader.discover(
            str(ROOT / "tests"), top_level_dir=str(ROOT)
        )
        self.assertFalse(expected_loader.errors)
        actual = []
        for shard in shards:
            loader = unittest.TestLoader()
            suite = loader.loadTestsFromNames(shard)
            self.assertFalse(loader.errors)
            actual.extend(test_ids(suite))
            for module in shard:
                ordered = test_ids(loader.loadTestsFromName(module))
                self.assertEqual(
                    ordered, [t for t in test_ids(suite) if t.startswith(module + ".")]
                )
        self.assertCountEqual(test_ids(expected), actual)
        self.assertEqual(len(actual), len(set(actual)))

    def test_new_modules_are_included_and_assignment_is_deterministic(self):
        modules = [
            "tests.test_new_feature",
            "tests.test_receipt_attach",
            "tests.test_allocations_crud",
        ]
        first = run_shard.partition_modules(modules, 2)
        self.assertEqual(first, run_shard.partition_modules(list(reversed(modules)), 2))
        self.assertEqual(1, sum("tests.test_new_feature" in shard for shard in first))

    def test_invalid_shard_configuration_fails_before_running(self):
        for args in (
            ["--index", "4", "--count", "4"],
            ["--index", "-1", "--count", "4"],
            ["--index", "0", "--count", "0"],
            ["--index", "0", "--count", "999999"],
        ):
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                with patch.object(unittest.TextTestRunner, "run") as execute:
                    with self.assertRaises(SystemExit) as error:
                        run_shard.main(args)
                    self.assertEqual(2, error.exception.code)
                    execute.assert_not_called()

    def test_an_empty_shard_cannot_report_success(self):
        with (
            patch.object(run_shard, "discover_modules", return_value=[]),
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(2, run_shard.main(["--index", "0", "--count", "1"]))

    def test_test_failure_propagates_as_nonzero(self):
        class FailingCase(unittest.TestCase):
            def runTest(self):
                self.fail("synthetic failure")

        with (
            patch.object(run_shard, "discover_modules", return_value=["synthetic"]),
            patch.object(
                unittest.TestLoader,
                "loadTestsFromNames",
                return_value=unittest.TestSuite([FailingCase()]),
            ),
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(1, run_shard.main(["--index", "0", "--count", "1"]))

    def test_import_failure_cannot_report_success(self):
        with (
            patch.object(
                run_shard,
                "discover_modules",
                return_value=["tests.missing_test_module"],
            ),
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(1, run_shard.main(["--index", "0", "--count", "1"]))

    def test_modules_without_cases_cannot_report_success(self):
        with (
            patch.object(
                run_shard, "discover_modules", return_value=["tests.test_browser_smoke"]
            ),
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(2, run_shard.main(["--index", "0", "--count", "1"]))

    def test_workflow_requires_all_shards_and_retains_other_checks(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        jobs = workflow["jobs"]
        matrix = jobs["docker-test-shards"]
        self.assertEqual([0, 1, 2, 3], matrix["strategy"]["matrix"]["shard"])
        self.assertFalse(matrix["strategy"]["fail-fast"])
        self.assertEqual(["docker-test-shards"], jobs["docker-tests"]["needs"])
        self.assertEqual("${{ always() }}", jobs["docker-tests"]["if"])
        self.assertEqual(
            "${{ github.event_name == 'pull_request' }}",
            workflow["concurrency"]["cancel-in-progress"],
        )
        self.assertEqual(
            "${{ github.workflow }}-${{ github.event.pull_request.number || github.run_id }}",
            workflow["concurrency"]["group"],
        )
        gate = jobs["docker-tests"]["steps"][0]
        self.assertEqual(
            "${{ needs.docker-test-shards.result }}", gate["env"]["SHARD_RESULT"]
        )
        for result, expected in (
            ("success", 0),
            ("failure", 1),
            ("skipped", 1),
            ("cancelled", 1),
        ):
            import os
            import subprocess

            process = subprocess.run(
                ["bash", "-c", gate["run"]],
                env={**os.environ, "SHARD_RESULT": result},
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                expected, process.returncode, process.stdout + process.stderr
            )
        for name in (
            "unit-tests",
            "macos-reliability",
            "browser-smoke-tests",
            "package-build",
        ):
            self.assertIn(name, jobs)
        self.assertTrue(all(job.get("timeout-minutes", 0) > 0 for job in jobs.values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
