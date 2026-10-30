"""Run the extension's Node regression suite in local and Docker unittest runs."""

from pathlib import Path
import re
import subprocess
import unittest

import playwright


class TestPiExtension(unittest.TestCase):
    def test_extension_argument_forwarding(self):
        root = Path(__file__).resolve().parent.parent
        node = Path(playwright.__file__).parent / "driver" / "node"
        result = subprocess.run(
            [
                str(node),
                "--experimental-vm-modules",
                "--test-reporter=tap",
                "--test",
                str(root / "tests" / "test_extension.mjs"),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout + result.stderr
        self.assertEqual(0, result.returncode, output)
        count = re.search(r"^# tests (\d+)$", result.stdout, re.MULTILINE)
        self.assertIsNotNone(count, output)
        self.assertGreater(int(count[1]), 0, "Node must actually execute tests")


if __name__ == "__main__":
    unittest.main(verbosity=2)
