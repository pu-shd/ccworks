"""Offline contracts for the distributable ccworks agent skill."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import re
import shlex
import tempfile
import unittest
from unittest.mock import patch

import yaml

from ccworks.cli import build_parser
from ccworks import cli

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "ccworks"
SKILL = PLUGIN / "skills" / "ccworks"


class TestCCWorksSkill(unittest.TestCase):
    def test_marketplace_resolves_the_portable_plugin(self):
        catalog = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
        self.assertEqual("ccworks-local", catalog["name"])
        (entry,) = catalog["plugins"]
        self.assertEqual("local", entry["source"]["source"])
        self.assertEqual(PLUGIN, (ROOT / entry["source"]["path"]).resolve())
        manifest = json.loads((PLUGIN / "plugin.json").read_text())
        self.assertEqual(entry["name"], manifest["name"])
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+(?:-[\w.]+)?$")
        self.assertEqual({"$schema", "name", "version", "description"}, set(manifest))

    def test_discovery_and_ui_metadata_are_valid(self):
        text = (SKILL / "SKILL.md").read_text()
        _, frontmatter, body = text.split("---", 2)
        metadata = yaml.safe_load(frontmatter)
        self.assertEqual({"name", "description"}, set(metadata))
        self.assertEqual(SKILL.name, metadata["name"])
        self.assertTrue(metadata["description"].strip())
        self.assertTrue(body.strip())
        ui = yaml.safe_load((SKILL / "agents/openai.yaml").read_text())
        self.assertTrue(25 <= len(ui["interface"]["short_description"]) <= 64)
        self.assertIn("$ccworks", ui["interface"]["default_prompt"])
        self.assertTrue(ui.get("policy", {}).get("allow_implicit_invocation", True))

    def test_bundle_is_self_contained_and_contains_only_skill_resources(self):
        expected = {
            "plugin.json",
            "skills/ccworks/SKILL.md",
            "skills/ccworks/agents/openai.yaml",
            "skills/ccworks/references/commands.md",
        }
        actual = {str(p.relative_to(PLUGIN)) for p in PLUGIN.rglob("*") if p.is_file()}
        self.assertEqual(expected, actual)
        for path in SKILL.rglob("*.md"):
            for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
                if "://" in target:
                    continue
                destination = (path.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(destination.is_relative_to(SKILL))
                self.assertTrue(destination.is_file(), target)

    def test_command_examples_parse_without_browser_or_shell_execution(self):
        parser, _ = build_parser()
        commands = []
        for path in SKILL.rglob("*.md"):
            for block in re.findall(
                r"```(?:zsh|bash|sh)\n(.*?)```", path.read_text(), re.S
            ):
                for line in block.replace("\\\n", " ").splitlines():
                    if not line.strip() or line.startswith("#"):
                        continue
                    argv = shlex.split(line)
                    self.assertEqual("ccworks", argv[0])
                    self.assertFalse(set(argv) & {";", "&&", "|", ">"})
                    if argv[1:] == ["--version"]:
                        with redirect_stdout(io.StringIO()):
                            with self.assertRaises(SystemExit) as result:
                                parser.parse_args(argv[1:])
                        self.assertEqual(0, result.exception.code)
                        continue
                    with self.subTest(command=argv[1:3]):
                        commands.append(parser.parse_args(argv[1:]))
        self.assertGreaterEqual(len(commands), 8)
        bulk = [
            c for c in commands if c.group == "txn" and c.subcommand == "allocate-bulk"
        ]
        self.assertEqual([True, False], [c.dry_run for c in bulk])
        self.assertEqual([1, 3], bulk[0].indices)
        self.assertEqual(bulk[0].indices, bulk[1].indices)

    def test_json_example_has_guarded_explicit_fields_and_no_allocation_clear(self):
        examples = re.findall(
            r"```json\n(.*?)```", (SKILL / "references/commands.md").read_text(), re.S
        )
        self.assertEqual(1, len(examples))
        payload = json.loads(examples[0])
        self.assertEqual("Statement Report", payload["report_name"])
        (expense,) = payload["expenses"]
        self.assertGreater(expense["index"], 0)
        self.assertTrue(expense["vendor"])
        self.assertRegex(expense["amount"], r"^\$\d+\.\d{2}$")
        self.assertEqual(expense["business_purpose"], expense["comment"])
        self.assertNotIn("allocation", expense)
        self.assertNotIn("receipt_file_path", expense)

    def test_documented_payload_dispatch_preserves_requested_fields_only(self):
        (example,) = re.findall(
            r"```json\n(.*?)```", (SKILL / "references/commands.md").read_text(), re.S
        )
        payload = json.loads(example)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "reconciliation.json"
            path.write_text(example)
            with (
                patch("sys.argv", ["ccworks", "report", "apply-json", str(path)]),
                patch.object(cli, "ConcurBrowserClient") as client,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                client.return_value.apply_json_updates.return_value = {"success": True}
                cli.main()
            client.return_value.apply_json_updates.assert_called_once_with(
                report_name=payload["report_name"],
                expenses=payload["expenses"],
                headless=True,
                ignore_session_budget=False,
            )
            self.assertEqual(
                ["apply_json_updates"],
                [call[0] for call in client.return_value.method_calls],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
