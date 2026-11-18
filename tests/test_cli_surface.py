#!/usr/bin/env python3
"""Invariants for the single-front-door command surface.

The parser is a front-end that normalizes `<group> <subcommand>` onto the
dispatcher's internal command tokens. The dangerous failure mode is drift: a
group/subcommand that maps to no dispatcher branch (runtime crash), or a
dispatcher branch no longer reachable from any command (dead code). The closure
test below pins both directions.
"""
import argparse
import pathlib
import re
import subprocess
import sys
import unittest
from argparse import Namespace
from unittest.mock import patch

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ccworks.cli import (  # noqa: E402
    LEGACY_COMMANDS,
    Spinner,
    _legacy_command,
    build_parser,
    split_incomplete_expenses,
)

CLI_SOURCE = (REPO_ROOT / "src" / "ccworks" / "cli.py").read_text()


class TestSpinner(unittest.TestCase):
    def test_progress_is_silent_for_redirected_and_desktop_commands(self):
        for interactive, frozen in ((False, False), (True, True)):
            with (
                self.subTest(interactive=interactive, frozen=frozen),
                patch.object(sys.stderr, "isatty", return_value=interactive),
                patch.object(sys, "frozen", frozen, create=True),
                patch("ccworks.cli.threading.Thread") as thread,
                Spinner("Example confidential report title"),
            ):
                thread.assert_not_called()

# Subcommands whose token depends on a flag, and the flags that switch them.
FLAG_DEPENDENT = {
    ("report", "list"): ("historical", ["query", "list-old-reports"]),
    ("report", "delete"): ("all_drafts", ["delete-report", "delete-all-reports"]),
}


def parser_surface():
    """-> {(group, subcommand_or_None)} for every command the parser accepts."""
    parser, _ = build_parser()
    top = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    surface = set()
    for group_name, group_parser in top.choices.items():
        nested = [a for a in group_parser._actions if isinstance(a, argparse._SubParsersAction)]
        if not nested:
            surface.add((group_name, None))
            continue
        for sub_name in nested[0].choices:
            surface.add((group_name, sub_name))
    return surface


def dispatcher_tokens():
    """-> {token} for every `args.command == "token"` branch in the dispatcher."""
    return set(re.findall(r'args\.command == "([a-z0-9-]+)"', CLI_SOURCE))


def producible_tokens():
    """-> {token} reachable from the parser surface, both flag variants included."""
    tokens = set()
    for group, sub in parser_surface():
        if (group, sub) in FLAG_DEPENDENT:
            attr, expected = FLAG_DEPENDENT[(group, sub)]
            for value in (False, True):
                ns = Namespace(group=group, subcommand=sub, **{attr: value})
                tokens.add(_legacy_command(ns))
            continue
        ns = Namespace(group=group, subcommand=sub)
        token = _legacy_command(ns)
        if token is not None:
            tokens.add(token)
    return tokens


class TestSurfaceClosure(unittest.TestCase):
    def test_every_command_maps_to_a_dispatcher_branch(self):
        unmapped = [
            (g, s) for g, s in parser_surface()
            if _legacy_command(Namespace(group=g, subcommand=s, historical=False, all_drafts=False)) is None
        ]
        self.assertEqual(
            [], unmapped,
            f"parser accepts commands that map to no dispatcher token: {unmapped}",
        )

    def test_no_orphaned_dispatcher_branches(self):
        orphans = dispatcher_tokens() - producible_tokens()
        self.assertEqual(
            set(), orphans,
            f"dispatcher handles tokens no command can produce (dead code): {sorted(orphans)}",
        )

    def test_no_unhandled_tokens(self):
        unhandled = producible_tokens() - dispatcher_tokens()
        self.assertEqual(
            set(), unhandled,
            f"commands produce tokens the dispatcher does not handle: {sorted(unhandled)}",
        )

    def test_flag_dependent_commands_resolve_both_ways(self):
        for (group, sub), (attr, expected) in FLAG_DEPENDENT.items():
            got = [
                _legacy_command(Namespace(group=group, subcommand=sub, **{attr: value}))
                for value in (False, True)
            ]
            self.assertEqual(expected, got, f"{group} {sub} --{attr}")


class TestRetiredNames(unittest.TestCase):
    def test_no_retired_name_collides_with_a_group(self):
        groups = {g for g, _ in parser_surface()}
        collisions = groups & set(LEGACY_COMMANDS)
        self.assertEqual(
            set(), collisions,
            f"retired names shadow live groups, so they'd be intercepted: {sorted(collisions)}",
        )

    def test_every_suggestion_is_a_real_command(self):
        surface = parser_surface()
        groups = {g for g, _ in surface}
        for old, suggestion in LEGACY_COMMANDS.items():
            parts = suggestion.split()
            self.assertIn(parts[0], groups, f"{old} -> unknown group '{parts[0]}'")
            if (parts[0], None) in surface:  # top-level command such as nuke
                continue
            self.assertIn(
                (parts[0], parts[1]), surface,
                f"{old} -> '{parts[0]} {parts[1]}' is not a real command",
            )

    def test_retired_names_exit_2_with_a_suggestion(self):
        for old in sorted(LEGACY_COMMANDS):
            proc = subprocess.run(
                [sys.executable, "-m", "ccworks.cli", old],
                capture_output=True, text=True, cwd=REPO_ROOT,
                env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT / "src")},
            )
            self.assertEqual(2, proc.returncode, f"{old}: expected exit 2, got {proc.returncode}")
            self.assertIn("unrecognized command", proc.stderr, old)
            self.assertIn("Did you mean", proc.stderr, old)
            self.assertIn(LEGACY_COMMANDS[old], proc.stderr, old)


class TestIncompleteRowsAreWithheldFromWrites(unittest.TestCase):
    """A row the capture flagged as incomplete must not be written back.

    When a transaction's detail pane fails to open, `report show --deep` returns
    it with business_purpose and comment empty and records it in
    extraction.deep_scan_failures. Writing those empties back clears whatever
    Concur actually holds -- silent data loss in the write direction.
    """

    CAPTURE = {
        "report_name": "Statement",
        "extraction": {
            "deep_scan_failures": [
                {"index": 1, "vendor": "APPLE.COM/BILL", "amount": "$2.99",
                 "reason": "detail pane did not open; shallow fields only"}
            ]
        },
        "expenses": [
            {"index": 1, "vendor": "APPLE.COM/BILL", "amount": "$2.99",
             "business_purpose": "", "comment": ""},
            {"index": 2, "vendor": "ANTHROPIC", "amount": "$45.00",
             "business_purpose": "Research", "comment": ""},
        ],
    }

    def test_flagged_row_is_withheld(self):
        safe, withheld = split_incomplete_expenses(self.CAPTURE["expenses"], self.CAPTURE)
        self.assertEqual([1], [e["index"] for e in withheld])
        self.assertEqual([2], [e["index"] for e in safe])

    def test_capture_with_no_failures_passes_everything(self):
        data = {"extraction": {"deep_scan_failures": []}}
        expenses = [{"index": 1}, {"index": 2}]
        safe, withheld = split_incomplete_expenses(expenses, data)
        self.assertEqual(expenses, safe)
        self.assertEqual([], withheld)

    def test_capture_without_an_extraction_block_is_passed_through(self):
        # JSON from before 0.3.0 has no extraction block; nothing to withhold.
        expenses = [{"index": 1}, {"index": 2}]
        safe, withheld = split_incomplete_expenses(expenses, {"report_name": "x"})
        self.assertEqual(expenses, safe)
        self.assertEqual([], withheld)

    def test_null_extraction_does_not_raise(self):
        safe, withheld = split_incomplete_expenses([{"index": 1}], {"extraction": None})
        self.assertEqual([{"index": 1}], safe)
        self.assertEqual([], withheld)


class TestVersionFlag(unittest.TestCase):
    """`--version` must report the installed version.

    Without it there was no way to tell which ccworks you were running, which
    made a stale Homebrew install indistinguishable from a broken one: the
    launcher tracks the working tree while the entry point tracks whatever
    release was installed.
    """

    def _run(self, *argv):
        return subprocess.run(
            [sys.executable, "-m", "ccworks.cli", *argv],
            capture_output=True, text=True, cwd=REPO_ROOT,
            env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        )

    def test_version_flag_prints_a_version_and_exits_zero(self):
        for flag in ("--version", "-V"):
            proc = self._run(flag)
            self.assertEqual(0, proc.returncode, f"{flag}: {proc.stderr}")
            out = (proc.stdout + proc.stderr).strip()
            self.assertRegex(out, r"^ccworks \S+$", f"{flag} printed {out!r}")

    def test_reported_version_matches_pyproject(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        declared = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE).group(1)
        reported = (self._run("--version").stdout + self._run("--version").stderr).strip()
        self.assertEqual(
            f"ccworks {declared}", reported,
            "the version read from installed metadata disagrees with pyproject; "
            "reinstall the package or the release is mislabelled",
        )

    def test_short_v_is_still_verbose_not_version(self):
        # -v predates -V and must keep meaning --verbose.
        proc = self._run("-v")
        self.assertNotRegex(proc.stdout.strip(), r"^ccworks \S+$")


class TestNoRetiredNamesInUserFacingText(unittest.TestCase):
    """Error messages must not tell users to run a command that no longer exists.

    The session-expiry error said "Please re-run the login command: ./ccworks
    login" well after `login` became `session login`, so following the
    instruction produced "unrecognized command". Docs were covered by a test;
    source strings were not.
    """

    SOURCE_FILES = [
        REPO_ROOT / "src" / "ccworks" / "browser_client.py",
        REPO_ROOT / "src" / "ccworks" / "cli.py",
        REPO_ROOT / "src" / "ccworks" / "client.py",
    ]

    def test_no_source_string_instructs_a_retired_command(self):
        # Match a retired name only where it reads as an instruction, i.e.
        # preceded by `ccworks ` -- so LEGACY_COMMANDS' own keys are not hits.
        pattern = re.compile(
            r"(?:\./)?ccworks\s+(" + "|".join(re.escape(c) for c in sorted(LEGACY_COMMANDS)) + r")\b"
        )
        offenders = []
        for path in self.SOURCE_FILES:
            if not path.exists():
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                for match in pattern.finditer(line):
                    # The retired -> replacement table legitimately names them.
                    if "LEGACY_COMMANDS" in line or "Did you mean" in line:
                        continue
                    offenders.append(f"{path.name}:{lineno}: {match.group(0)}")
        self.assertEqual(
            [], offenders,
            "user-facing text instructs a retired command: " + "; ".join(offenders),
        )


class TestGuards(unittest.TestCase):
    """Flag combinations that must be refused rather than silently misfire."""

    def _run(self, *argv):
        return subprocess.run(
            [sys.executable, "-m", "ccworks.cli", *argv],
            capture_output=True, text=True, cwd=REPO_ROOT,
            env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        )

    def test_receipt_delete_requires_all(self):
        proc = self._run("receipt", "delete")
        self.assertEqual(2, proc.returncode)
        self.assertIn("requires --all", proc.stderr)

    def test_report_delete_needs_a_target(self):
        proc = self._run("report", "delete")
        self.assertEqual(2, proc.returncode)
        self.assertIn("needs a report NAME", proc.stderr)

    def test_report_delete_rejects_both_targets(self):
        proc = self._run("report", "delete", "R", "--all-drafts")
        self.assertEqual(2, proc.returncode)
        self.assertIn("not both", proc.stderr)

    def test_view_without_historical_is_refused(self):
        # Silently ignoring --view here would misreport which reports were listed.
        proc = self._run("report", "list", "--view", "All Reports")
        self.assertEqual(2, proc.returncode)
        self.assertIn("--historical", proc.stderr)

    def test_bare_invocation_prints_reference_and_exits_zero(self):
        proc = self._run()
        self.assertEqual(0, proc.returncode)
        self.assertIn("report list", proc.stderr)

    def test_group_without_subcommand_shows_group_help(self):
        proc = self._run("report")
        self.assertEqual(2, proc.returncode)
        self.assertIn("reconcile", proc.stderr)


class TestHelpRendering(unittest.TestCase):
    """Subcommand help must not inherit the top-level usage string.

    argparse before 3.14 derives a subparser's prog prefix from the parent's
    `usage=` when one is set, rather than from `prog`. Without an explicit prog
    on add_subparsers, every subcommand rendered as

        usage: ccworks [-h] [-v] [--output {json,text}] <command> [args...] report list

    This passes trivially on 3.14 and fails on 3.10-3.13, which is what CI and
    the Homebrew formula run -- so assert the exact prefix rather than just
    "looks fine here".
    """

    def _help_first_line(self, *argv):
        proc = subprocess.run(
            [sys.executable, "-m", "ccworks.cli", *argv, "--help"],
            capture_output=True, text=True, cwd=REPO_ROOT,
            env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        return proc.stdout.splitlines()[0]

    def test_subcommand_usage_starts_with_its_own_path(self):
        for argv in (("report", "list"), ("txn", "allocate"), ("card", "show"),
                     ("session", "status")):
            expected = "usage: ccworks " + " ".join(argv)
            first = self._help_first_line(*argv)
            self.assertTrue(
                first.startswith(expected),
                f"expected help to start with {expected!r}, got {first!r}",
            )

    def test_subcommand_usage_omits_global_flags(self):
        first = self._help_first_line("report", "list")
        for leaked in ("--output", "<command>", "[args...]"):
            self.assertNotIn(
                leaked, first,
                f"top-level usage leaked into subcommand help: {first!r}",
            )

    def test_group_usage_starts_with_its_own_path(self):
        first = self._help_first_line("report")
        self.assertTrue(first.startswith("usage: ccworks report"), first)


if __name__ == "__main__":
    unittest.main()
