"""A failing desktop smoke check must show what the helper actually returned."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "desktop_smoke", ROOT / "desktop/smoke.py"
)
assert spec and spec.loader
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

RELEASE = "9.9.9"
UNSUPPORTED = {
    "protocol": 1,
    "version": RELEASE,
    "browser": "unsupported",
    "sign_in": "unchecked",
}


def completed(args, returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class SmokeDiagnosticsTests(unittest.TestCase):
    def run_smoke(self, chrome_result, extra_args=("--fresh-chrome",)):
        """Run main() with healthy version/desktop checks and a given Chrome result."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[1:] == ["--version"]:
                return completed(cmd, 0, f"ccworks {RELEASE}\n")
            if kwargs["env"].get("CCWORKS_BROWSER") == "chrome":
                if isinstance(chrome_result, Exception):
                    raise chrome_result
                return chrome_result
            return completed(cmd, 1, json.dumps(UNSUPPORTED))

        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "ccworks"
            helper.write_text("")
            with (
                patch.object(smoke, "version", return_value=RELEASE),
                patch.object(smoke.subprocess, "run", side_effect=fake_run),
                patch("ccworks.paths.session_file") as session_file,
                patch("sys.argv", ["smoke.py", str(helper), *extra_args]),
                patch("builtins.print"),
            ):
                session_file.return_value = Path(tmp) / "absent.json"
                smoke.main()
        return calls

    def test_unexpected_chrome_result_reports_exit_code_and_output(self):
        result = completed(
            ["ccworks"],
            1,
            json.dumps({**UNSUPPORTED, "browser": "missing"}),
            "Chrome executable not found",
        )
        with self.assertRaises(RuntimeError) as caught:
            self.run_smoke(result)
        message = str(caught.exception)
        self.assertIn("could not check installed Chrome", message)
        self.assertIn("exit code 1", message)
        self.assertIn('"browser": "missing"', message)
        self.assertIn("Chrome executable not found", message)
        self.assertIn('"browser": "ready"', message, "show the expected result too")

    def test_non_json_output_is_reported_instead_of_a_decode_error(self):
        result = completed(["ccworks"], 1, "Traceback: driver crashed", "")
        with self.assertRaises(RuntimeError) as caught:
            self.run_smoke(result)
        self.assertIn("Traceback: driver crashed", str(caught.exception))

    def test_timeout_reports_partial_output(self):
        timeout = subprocess.TimeoutExpired(
            ["ccworks"], 45, output="partial stdout", stderr="still launching"
        )
        with self.assertRaises(RuntimeError) as caught:
            self.run_smoke(timeout)
        message = str(caught.exception)
        self.assertIn("timed out after 45", message)
        self.assertIn("partial stdout", message)
        self.assertIn("still launching", message)

    def test_long_output_is_truncated(self):
        result = completed(["ccworks"], 1, "x" * 10000, "")
        with self.assertRaises(RuntimeError) as caught:
            self.run_smoke(result)
        self.assertLess(len(str(caught.exception)), 6000)

    def test_expected_results_still_pass(self):
        ready = completed(
            ["ccworks"],
            1,
            json.dumps({**UNSUPPORTED, "browser": "ready", "sign_in": "required"}),
        )
        calls = self.run_smoke(ready)
        self.assertEqual(3, len(calls), "version, desktop and Chrome checks all ran")


if __name__ == "__main__":
    unittest.main(verbosity=2)
