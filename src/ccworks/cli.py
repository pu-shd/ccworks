#!/usr/bin/env python3
import os
import sys
import argparse
import json
import threading
import itertools
import time
import logging
import signal
from datetime import datetime
from dotenv import load_dotenv

# Configure signal handling for graceful exit
def signal_handler(sig, frame):
    logging.info(f"Signal {sig} received. Cleaning up and exiting...")
    sys.exit(0)

signal.signal(signal.SIGHUP, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

from ccworks import __version__
from ccworks.client import ConcurClient, ConcurError
from ccworks.browser_client import ConcurBrowserClient, ConcurSessionExpiredError


def handle_session_expired(e):
    """Handles session expiration by offering an interactive login re-run."""
    # Print error to stderr so it doesn't corrupt JSON stdout
    print(f"\n[SESSION EXPIRED] {str(e)}", file=sys.stderr)
    
    # Only offer interactive login if we are in a TTY
    if sys.stdin.isatty():
        try:
            sys.stderr.write("\nWould you like to run the login command now? (y/N): ")
            sys.stderr.flush()
            choice = sys.stdin.readline().strip().lower()
            if choice == 'y':
                print("\nLaunching browser for login...", file=sys.stderr)
                client = ConcurBrowserClient()
                client.run_headed_login()
                print("\n[INFO] Login complete. Resuming your previous command...\n", file=sys.stderr)
                os.execv(sys.executable, [sys.executable] + sys.argv)
        except (EOFError, KeyboardInterrupt):
            print("\nLogin skipped.", file=sys.stderr)
    
    # If not interactive or user declined, exit with error
    # For JSON output compatibility, we still print the error to stdout if that's where results go
    # but the specific commands already handle the JSON error response.
    sys.exit(1)


class Spinner:
    """A simple terminal spinner for long-running tasks."""
    def __init__(self, message="In progress...", delay=0.1):
        self.spinner = itertools.cycle(['-', '/', '|', '\\'])
        self.delay = delay
        self.message = message
        self.running = False
        self.thread = None

    def spin(self):
        while self.running:
            sys.stderr.write(f"\r{next(self.spinner)} {self.message}")
            sys.stderr.flush()
            time.sleep(self.delay)
        sys.stderr.write("\r" + " " * (len(self.message) + 2) + "\r")
        sys.stderr.flush()

    def __enter__(self):
        self.running = True
        self.thread = threading.Thread(target=self.spin)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.running = False
        if self.thread:
            self.thread.join()


# ---------------------------------------------------------------------------
# Command surface
#
# One front door: resource groups with verb subcommands (`ccworks report list`),
# following the convention used by gh/kubectl/docker. The parser below is a
# front-end that normalizes (group, subcommand, flags) into the legacy internal
# command tokens the dispatcher already switches on, so the dispatcher bodies
# stay untouched.
# ---------------------------------------------------------------------------

# Old flat names -> the new invocation that replaces them. Removed outright
# (hard break), but we intercept them to emit a pointed error rather than
# argparse's bare "invalid choice".
LEGACY_COMMANDS = {
    # former Python-CLI names
    "api-test": "api test",
    "login": "session login",
    "check-session": "session status",
    "query": "report list",
    "list-old-reports": "report list --historical",
    "report-details": "report show NAME",
    "create-report": "report create",
    "update-report": "report update NAME",
    "submit-report": "report submit NAME",
    "delete-report": "report delete NAME",
    "delete-all-reports": "report delete --all-drafts",
    "delete-all-receipts": "receipt delete --all",
    "reconcile": "report reconcile NAME",
    "apply-json": "report apply-json PATH",
    "allocations": "txn allocations NAME",
    "add-allocation": "txn allocate NAME INDEX --dept D --fund F",
    "update-transaction": "txn update NAME INDEX...",
    "attach-receipt": "txn attach-receipt NAME --merchant M --file F",
    "list-cards": "card list",
    "card-details": "card show MERCHANT",
    "add-delegate": "delegate add WHO",
    "remove-delegate": "delegate remove WHO",
    # former launcher-only names
    "query-old": "report list --historical",
    "create": "report create",
    "create-headed": "report create --headed",
    "delete": "report delete NAME",
    "run-live": "api test",
}

# (group, subcommand) -> legacy internal token. Entries whose token depends on a
# flag are resolved in _legacy_command().
_COMMAND_MAP = {
    ("report", "show"): "report-details",
    ("report", "create"): "create-report",
    ("report", "update"): "update-report",
    ("report", "submit"): "submit-report",
    ("report", "reconcile"): "reconcile",
    ("report", "apply-json"): "apply-json",
    ("txn", "update"): "update-transaction",
    ("txn", "allocations"): "allocations",
    ("txn", "unallocate"): "unallocate",
    ("txn", "detach-receipt"): "detach-receipt",
    ("txn", "allocate"): "add-allocation",
    ("txn", "attach-receipt"): "attach-receipt",
    ("card", "list"): "list-cards",
    ("card", "show"): "card-details",
    ("delegate", "add"): "add-delegate",
    ("delegate", "remove"): "remove-delegate",
    ("receipt", "delete"): "delete-all-receipts",
    ("session", "login"): "login",
    ("session", "status"): "check-session",
    ("api", "test"): "api-test",
}


def split_incomplete_expenses(expenses, data):
    """Split expenses into (safe_to_write, withheld) using the capture's own report.

    A row listed in extraction.deep_scan_failures has empty business_purpose and
    comment because its detail pane never opened, not because Concur holds them
    empty. Writing those values back clears real data, so they are withheld by
    default. Pure function so this is unit-testable without driving a browser.
    """
    failed = {
        f.get("index")
        for f in (data.get("extraction") or {}).get("deep_scan_failures", [])
        if f.get("index") is not None
    }
    if not failed:
        return list(expenses), []
    safe = [e for e in expenses if e.get("index") not in failed]
    withheld = [e for e in expenses if e.get("index") in failed]
    return safe, withheld


def _legacy_command(args) -> str:
    """Map the parsed group/subcommand onto the dispatcher's command token."""
    group = getattr(args, "group", None)
    if group == "nuke":
        return "nuke"
    sub = getattr(args, "subcommand", None)
    if (group, sub) == ("report", "list"):
        return "list-old-reports" if args.historical else "query"
    if (group, sub) == ("report", "delete"):
        return "delete-all-reports" if args.all_drafts else "delete-report"
    return _COMMAND_MAP.get((group, sub))


def _command_reference() -> str:
    """Hand-formatted reference shown in `ccworks --help` / bare `ccworks`."""
    rows = [
        ("report", None),
        ("report list",           "List draft reports [--historical] [--view F]"),
        ("report show",           "Detail a report by name [--deep] [--view F]"),
        ("report create",         "Create a draft report [--name --purpose --comment --headed]"),
        ("report update",         "Update header fields [--name --purpose --comment --justification]"),
        ("report reconcile",      "Reconcile transactions [--rules F] [--submit]"),
        ("report submit",         "Submit a report for approval"),
        ("report delete",         "Delete a report by name, or --all-drafts"),
        ("report apply-json",     "Apply an edited `report show` JSON back to Concur"),
        ("txn", None),
        ("txn update",            "Update transactions by 1-based index [--type --justification ...]"),
        ("txn allocations",       "List chartstring allocations for a report [--view F]"),
        ("txn allocate",          "Add a chartstring to a transaction (--dept --fund [--prog])"),
        ("txn attach-receipt",    "Attach a local receipt file (--merchant --file)"),
        ("card", None),
        ("card list",             "List credit-card transactions [--view F]"),
        ("card show",             "Detail a card transaction by merchant or ID [--view F]"),
        ("receipt", None),
        ("receipt delete",        "Delete available receipts (--all)"),
        ("delegate", None),
        ("delegate add",          "Add an expense delegate [--can prepare submit approve]"),
        ("delegate remove",       "Remove an expense delegate"),
        ("session", None),
        ("session login",         "Launch a headed browser for manual authentication"),
        ("session status",        "Check whether the saved session is still valid"),
        ("api", None),
        ("api test",              "Run the API client test suite (requires .env OAuth creds)"),
        ("nuclear", None),
        ("nuke",                  "Delete ALL draft reports AND all available receipts"),
    ]
    lines = ["Commands:"]
    for name, desc in rows:
        if desc is None:
            lines.append("")
            lines.append(f"  {name}:")
        else:
            lines.append(f"    {name:<22} {desc}")
    lines.append("")
    lines.append("Run `ccworks <group> <subcommand> --help` for per-command flags.")
    lines.append("Environment: CCWORKS_STATE_DIR overrides ~/Library/Application Support/ccworks.")
    return "\n".join(lines)


def build_parser():
    """Construct the argument parser and the per-group parsers.

    Split out of run_tests() so tests can introspect the command surface
    without invoking the dispatcher.
    """
    parser = argparse.ArgumentParser(
        prog="ccworks",
        description="SAP Concur API & Browser Access Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_command_reference(),
        usage="ccworks [-h] [-V] [-v] [--output {json,text}] <command> [args...]",
    )
    # -V, not -v: -v is --verbose. Knowing the installed version matters here
    # because the launcher tracks the working tree while the entry point tracks
    # whatever release was installed, and the two can be far apart.
    parser.add_argument("-V", "--version", action="version", version=f"ccworks {__version__}",
                        help="Show the installed ccworks version and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed log messages on stderr")
    parser.add_argument("--output", choices=["json", "text"], default="json",
                        help="Output format (default: json for queries)")

    # `required=False` so bare `ccworks` prints our formatted reference instead
    # of argparse's "the following arguments are required" error.
    # `prog` must be passed explicitly. On Python < 3.14 argparse derives the
    # subparser prog prefix from the parent's `usage=` string when one is set,
    # so every subcommand's help rendered as
    #   usage: ccworks [-h] [-v] [--output {json,text}] <command> [args...] report list
    # Python 3.14 derives it from `prog` instead, which is why this only shows up
    # on the versions most users actually run.
    subparsers = parser.add_subparsers(dest="group", required=False, metavar="<command>",
                                       prog="ccworks", help=argparse.SUPPRESS)

    group_parsers = {}

    def add_group(name, help_text):
        gp = subparsers.add_parser(name, help=help_text)
        gsub = gp.add_subparsers(dest="subcommand", required=False, metavar="<subcommand>")
        group_parsers[name] = gp
        return gsub

    # ---------------- report ----------------
    report = add_group("report", "Expense reports")

    r_list = report.add_parser("list", help="List reports (drafts by default)")
    r_list.add_argument("--historical", action="store_true",
                        help="List historical/processed reports instead of drafts")
    r_list.add_argument("--view", dest="filter_view", type=str, default=None,
                        help="Dropdown filter for --historical (default: 'Last 90 Days')")

    r_show = report.add_parser("show", help="Detailed view of a report by name")
    r_show.add_argument("report_name", type=str, help="Name of the expense report")
    r_show.add_argument("--deep", action="store_true",
                        help="Deep scan: open each transaction for full details")
    r_show.add_argument("--view", dest="filter_view", type=str, default=None,
                        help="Dropdown filter to look inside")

    r_create = report.add_parser("create", help="Create a draft expense report")
    r_create.add_argument("--name", type=str, help="Name of report to create")
    r_create.add_argument("--purpose", type=str, help="Business purpose of report to create")
    r_create.add_argument("--comment", type=str, help="Additional comment for report to create")
    r_create.add_argument("--headed", action="store_true",
                          help="Run browser visibly rather than headlessly")

    r_update = report.add_parser("update", help="Update a report's header fields")
    r_update.add_argument("report_name", type=str, help="Current name of the expense report")
    r_update.add_argument("--name", type=str, help="New name for the report")
    r_update.add_argument("--purpose", type=str, help="New business purpose")
    r_update.add_argument("--comment", type=str, help="New comment")
    r_update.add_argument("--justification", type=str,
                          help="Set both purpose and comment to the same text")

    r_recon = report.add_parser("reconcile", help="Reconcile a report's transactions")
    r_recon.add_argument("report_name", type=str, help="Name of draft report to reconcile")
    r_recon.add_argument("--rules", dest="reconcile_rules", type=str, metavar="PATH",
                         help="Path to a JSON file of reconciliation rules")
    r_recon.add_argument("--submit", action="store_true",
                         help="Submit after reconciling (default: review-only)")

    r_submit = report.add_parser("submit", help="Submit an expense report for approval")
    r_submit.add_argument("report_name", type=str, help="Name of the expense report to submit")

    r_delete = report.add_parser("delete", help="Delete a report by name, or every draft")
    r_delete.add_argument("report_name", type=str, nargs="?", help="Name of report to delete")
    r_delete.add_argument("--all-drafts", action="store_true",
                          help="Delete every draft expense report")

    r_apply = report.add_parser("apply-json", help="Apply edited report JSON back to Concur")
    r_apply.add_argument("json_path", type=str, help="Path to the edited JSON file")
    r_apply.add_argument("--include-incomplete", action="store_true",
                         help="Also write rows listed in extraction.deep_scan_failures, whose "
                              "captured fields are known to be incomplete (default: skip them)")
    r_apply.add_argument("--ignore-session-budget", action="store_true",
                         help="Start even when the Concur session looks too short to "
                              "finish. Without this, a payload that cannot complete is "
                              "refused up front, and a run stops cleanly before the "
                              "session expires rather than failing mid-row")
    r_apply.add_argument("--headed", action="store_true",
                         help="Run the browser visibly rather than headlessly, to watch what "
                              "a write actually does in Concur. Diagnostic only: the write "
                              "path behaves identically either way")

    # ---------------- txn ----------------
    txn = add_group("txn", "Transactions within a report")

    t_update = txn.add_parser("update", help="Update fields on transactions inside a report")
    t_update.add_argument("report_name", type=str, help="Name of the expense report")
    t_update.add_argument("transaction_indices", type=int, nargs="+",
                          help="1-based indices of transaction rows (e.g. 1 2 5)")
    t_update.add_argument("--type", type=str, help="Expense Type")
    t_update.add_argument("--purpose", type=str, help="Business Purpose")
    t_update.add_argument("--comment", type=str, help="Comment")
    t_update.add_argument("--justification", type=str,
                          help="Set both purpose and comment to the same text")

    t_allocs = txn.add_parser("allocations", help="List chartstring allocations for a report")
    t_allocs.add_argument("report_name", type=str, help="Name of the expense report")
    t_allocs.add_argument("--view", dest="filter_view", type=str, default=None,
                          help="Dropdown filter to look inside")

    t_alloc = txn.add_parser("allocate", help="Add a chartstring allocation to a transaction")
    t_alloc.add_argument("report_name", type=str, help="Name of the expense report")
    t_alloc.add_argument("index", type=int, help="1-based index of the transaction row")
    t_alloc.add_argument("--dept", type=str, required=True,
                         help="Department (e.g. '(25605) ORF-Technical Support')")
    t_alloc.add_argument("--fund", type=str, required=True,
                         help="Fund (e.g. '(A0001) General Fund')")
    t_alloc.add_argument("--prog", type=str, help="Program (e.g. '(P999) Research')")

    t_unalloc = txn.add_parser("unallocate",
                               help="Clear every allocation on a transaction")
    t_unalloc.add_argument("report_name", type=str, help="Name of the expense report")
    t_unalloc.add_argument("index", type=int, help="1-based index of the transaction row")

    t_detach = txn.add_parser("detach-receipt",
                              help="Remove the receipt from a transaction")
    t_detach.add_argument("report_name", type=str, help="Name of the expense report")
    t_detach.add_argument("index", type=int, help="1-based index of the transaction row")

    t_attach = txn.add_parser("attach-receipt", help="Attach a receipt file to a transaction")
    t_attach.add_argument("report_name", type=str,
                          help="Name of report containing the transaction")
    t_attach.add_argument("--merchant", type=str, required=True,
                          help="Merchant name or transaction ID to match")
    t_attach.add_argument("--file", dest="receipt_path", type=str, required=True, metavar="PATH",
                          help="Local file path of the receipt")

    # ---------------- card ----------------
    card = add_group("card", "Credit-card transactions")

    c_list = card.add_parser("list", help="List credit card transactions")
    c_list.add_argument("--view", dest="filter_view", type=str,
                        default="All Corporate and Personal Cards",
                        help="Dropdown filter (default: 'All Corporate and Personal Cards')")

    c_show = card.add_parser("show", help="Detailed view of a card transaction")
    c_show.add_argument("merchant_or_id", type=str, help="Merchant name or transaction ID")
    c_show.add_argument("--view", dest="filter_view", type=str,
                        default="All Corporate and Personal Cards",
                        help="Dropdown filter (default: 'All Corporate and Personal Cards')")

    # ---------------- receipt ----------------
    receipt = add_group("receipt", "Available receipts")
    rc_delete = receipt.add_parser("delete", help="Delete available receipts")
    rc_delete.add_argument("--all", dest="all_receipts", action="store_true",
                           help="Delete every available receipt (required)")

    # ---------------- delegate ----------------
    delegate = add_group("delegate", "Expense delegates")
    d_add = delegate.add_parser("add", help="Add a new expense delegate")
    d_add.add_argument("name_or_email", type=str, help="Name or email of delegate")
    d_add.add_argument("--can", dest="delegate_perms", nargs="+", default=["prepare"],
                       metavar="PERM",
                       help="Permissions: prepare, submit, approve (default: prepare)")
    d_remove = delegate.add_parser("remove", help="Remove an expense delegate")
    d_remove.add_argument("name_or_email", type=str, help="Name or email of delegate")

    # ---------------- session ----------------
    session = add_group("session", "Authentication session")
    session.add_parser("login", help="Launch a headed browser for manual authentication")
    session.add_parser("status", help="Check whether the saved session is still valid")

    # ---------------- api ----------------
    api = add_group("api", "Direct API access")
    api.add_parser("test", help="Run the API client test suite")

    # ---------------- nuke ----------------
    subparsers.add_parser("nuke", help="Delete all draft reports AND all available receipts")

    return parser, group_parsers


def run_tests():
    # Load .env file
    load_dotenv()

    # Hard break on the retired flat command names. Scan the original argv
    # (before the global-flag hoisting below) so `--output text query` resolves
    # to `query` rather than to the flag's value.
    _scan = sys.argv[1:]
    _idx = 0
    _first = None
    while _idx < len(_scan):
        _arg = _scan[_idx]
        if _arg == "--output":
            _idx += 2
        elif _arg.startswith("-"):
            _idx += 1
        else:
            _first = _arg
            break
    if _first in LEGACY_COMMANDS:
        print(f"ccworks: error: unrecognized command '{_first}'", file=sys.stderr)
        print(f"\n  Did you mean:  ccworks {LEGACY_COMMANDS[_first]}\n", file=sys.stderr)
        print("Run `ccworks --help` for the full command reference.", file=sys.stderr)
        sys.exit(2)

    # Preprocess sys.argv to move global arguments (--output, -v, --verbose) right after the script name.
    # This prevents argparse unrecognized argument errors when they are placed after subcommands.
    new_argv = [sys.argv[0]]
    global_args = []
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--output":
            if i + 1 < len(sys.argv):
                global_args.extend([arg, sys.argv[i+1]])
                i += 2
            else:
                global_args.append(arg)
                i += 1
        elif arg in ("-v", "--verbose"):
            global_args.append(arg)
            i += 1
        else:
            new_argv.append(arg)
            i += 1
    sys.argv = [new_argv[0]] + global_args + new_argv[1:]

    parser, group_parsers = build_parser()

    args = parser.parse_args()

    # Bare `ccworks` (or `ccworks -v` etc.): show the friendly command
    # reference instead of argparse's terse error.
    if args.group is None:
        parser.print_help(sys.stderr)
        sys.exit(0)

    # `nuke` is a top-level command and has no subcommand attribute at all.
    sub = getattr(args, "subcommand", None)

    # A group with no subcommand (`ccworks report`) is a usage error, but the
    # group's own help is the useful thing to show.
    if args.group in group_parsers and sub is None:
        group_parsers[args.group].print_help(sys.stderr)
        sys.exit(2)

    # Reject flag combinations that would otherwise be silently ignored or
    # dangerously broad.
    if (args.group, sub) == ("report", "list"):
        if args.filter_view and not args.historical:
            parser.error("--view applies only to `report list --historical`")
    if (args.group, sub) == ("report", "delete"):
        if args.all_drafts and args.report_name:
            parser.error("pass a report NAME or --all-drafts, not both")
        if not args.all_drafts and not args.report_name:
            parser.error("`report delete` needs a report NAME, or --all-drafts to remove every draft")
    if (args.group, sub) == ("receipt", "delete") and not args.all_receipts:
        parser.error("`receipt delete` requires --all (there is no per-receipt delete)")

    # Normalize the new surface onto the dispatcher's internal command tokens.
    args.command = _legacy_command(args)
    if args.command is None:  # unreachable unless a parser lacks a mapping
        parser.error(f"unhandled command: {args.group} {sub}")

    # Defaults the dispatcher expects, now that --view is shared across groups.
    if args.command == "list-old-reports" and not args.filter_view:
        args.filter_view = "Last 90 Days"

    # Configure logging based on verbosity
    log_level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
        force=True
    )
    # Ensure all loggers use stderr and respect the level
    for name in logging.root.manager.loggerDict:
        l = logging.getLogger(name)
        l.setLevel(log_level)
        l.propagate = True

    def output_result(data, text_summary=None):
        if args.output == "json":
            print(json.dumps(data, indent=2))
        elif text_summary:
            print(text_summary)
        else:
            print(json.dumps(data, indent=2))

    # ----------------------------------------------------
    # Command Dispatcher
    # ----------------------------------------------------
    try:
        if args.command == "api-test":
            client_id = os.getenv("CONCUR_CLIENT_ID")
            client_secret = os.getenv("CONCUR_CLIENT_SECRET")
            token_url = os.getenv("CONCUR_TOKEN_URL", "https://us.api.concursolutions.com/oauth2/v0/token")
            base_url = os.getenv("CONCUR_BASE_URL", "https://us.api.concursolutions.com")
            user_login_id = os.getenv("CONCUR_USER_LOGIN_ID")

            if args.output == "text":
                print("=" * 60)
                print("           SAP Concur API Access Tester Script")
                print("=" * 60)

            missing_vars = []
            if not client_id or client_id == "your_client_id_here":
                missing_vars.append("CONCUR_CLIENT_ID")
            if not client_secret or client_secret == "your_client_secret_here":
                missing_vars.append("CONCUR_CLIENT_SECRET")
            if not user_login_id or user_login_id == "user@example.com":
                missing_vars.append("CONCUR_USER_LOGIN_ID")

            if missing_vars:
                if args.output == "text":
                    print("\n[!] Configuration Missing.")
                    print("Please configure your credentials in the '.env' file.")
                    print("Required variables missing:")
                    for var in missing_vars:
                        print(f"  - {var}")
                    print("\nYou can copy '.env.example' to '.env' and update it:")
                    print("  cp .env.example .env")
                    print("=" * 60)
                else:
                    print(json.dumps({"status": "error", "missing_vars": missing_vars}))
                sys.exit(1)

            if args.output == "text":
                print(f"[*] Base URL:  {base_url}")
                print(f"[*] Token URL: {token_url}")
                print(f"[*] Test User: {user_login_id}")
                print(f"[*] Client ID: {client_id[:6]}... (truncated)")
                print("-" * 60)

            try:
                client = ConcurClient(
                    client_id=client_id,
                    client_secret=client_secret,
                    token_url=token_url,
                    base_url=base_url
                )

                if args.output == "text": print("\n[Phase 1] Attempting authentication...")
                token = client.get_token()
                if args.output == "text":
                    print("[SUCCESS] Authentication succeeded!")
                    print(f"          Access token acquired (starts with: '{token[:12]}...')")

                if args.output == "text": print("\n[Phase 2] Attempting to list existing reports...")
                reports = client.list_reports(user_login_id=user_login_id, limit=5)
                if args.output == "text":
                    print("[SUCCESS] Successfully connected to report list API!")
                    print(f"          Retrieved {len(reports)} recent report(s):")
                    for idx, report in enumerate(reports, 1):
                        report_name_val = report.get("Name", "Unnamed Report")
                        report_id = report.get("ReportID") or report.get("ID") or "N/A"
                        report_status = report.get("ReportStatus") or report.get("ApprovalStatus") or "N/A"
                        total = report.get("Total", 0.0)
                        currency = report.get("CurrencyCode", "")
                        print(f"            {idx}. [{report_id}] {report_name_val} - Status: {report_status} ({total} {currency})")

                if args.output == "text": print("\n[Phase 3] Attempting to create draft report...")
                report_name_val = f"API Test Draft {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                purpose = "Validating programmatic creation of draft reports"
                comment = "Created automatically via SAP Concur Python API Access Tester"

                created_report = client.create_draft_report(
                    user_login_id=user_login_id,
                    name=report_name_val,
                    purpose=purpose,
                    comment=comment
                )

                if args.output == "text":
                    print("[SUCCESS] Programmatic report creation succeeded!")
                    print(f"          New Report Name: {created_report.get('Name')}")
                    print(f"          Report ID:       {created_report.get('ReportID') or created_report.get('ID')}")
                    print(f"          Status:          {created_report.get('ReportStatus', 'Draft / Not Submitted')}")
                    print("-" * 60)
                    print("\n[SUMMARY] All API tests passed! You have full read/write access.")
                else:
                    print(json.dumps({
                        "status": "success",
                        "reports_retrieved": len(reports),
                        "created_report": created_report
                    }, indent=2))

            except ConcurError as e:
                if args.output == "text":
                    print(f"\n[ERROR] An API error occurred during testing: {str(e)}")
                else:
                    print(json.dumps({"status": "error", "type": "ConcurError", "message": str(e)}))
                sys.exit(1)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[UNEXPECTED ERROR] An unexpected error occurred: {str(e)}")
                else:
                    print(json.dumps({"status": "error", "type": "UnexpectedError", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow B: Browser Manual Login Session Save
        # ----------------------------------------------------
        elif args.command == "login":
            if args.output == "text":
                print("=" * 60)
                print("       SAP Concur Browser Authentication Session Setup")
                print("=" * 60)
            
            try:
                browser_client = ConcurBrowserClient()
                browser_client.run_headed_login()
                
                result = {"status": "success", "message": "Manual login setup complete."}
                summary = "\n[SUCCESS] Setup complete. You can now run browser-based automations.\nTo create a draft report, use: ccworks report create"
                output_result(result, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to run manual login setup: {str(e)}")
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow B.2: Browser Check Session Validity
        # ----------------------------------------------------
        elif args.command == "check-session":
            if args.output == "text":
                print("=" * 60)
                print("     SAP Concur Browser Session Status Check")
                print("=" * 60)
            try:
                with Spinner("Checking browser session..."):
                    browser_client = ConcurBrowserClient()
                    result = browser_client.check_session_validity(headless=True)
                
                if result.get("authenticated"):
                    summary = f"\n[SUCCESS] Authentication is active and valid!\nDetail: {result.get('reason')}\n" + "=" * 60
                    output_result(result, summary)
                else:
                    summary = f"\n[EXPIRED/NOT FOUND] Authentication is NOT valid.\nDetail: {result.get('reason')}\n" + "=" * 60
                    output_result(result, summary)
                    sys.exit(2)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to execute session status check: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow C: Browser Query (List Reports + List Receipts)
        # ----------------------------------------------------
        elif args.command == "query":
            if args.output == "text":
                print("=" * 60)
                print("     SAP Concur Browser-Based Expense & Receipt Query")
                print("=" * 60)
            
            try:
                with Spinner("Querying reports and receipts..."):
                    browser_client = ConcurBrowserClient()
                    reports = browser_client.list_reports(headless=True)
                    receipts = browser_client.list_available_receipts(headless=True)
                
                result = {
                    "reports": reports,
                    "receipts": receipts
                }
                
                summary = "\n[*] Querying active expense reports...\n"
                summary += f"[SUCCESS] Discovered {len(reports)} expense report(s):\n"
                for idx, r in enumerate(reports, 1):
                    summary += f"  {idx}. {r.get('name')} (Purpose: {r.get('purpose', 'None')})\n"
                
                summary += "\n[*] Querying available receipts gallery...\n"
                summary += f"[SUCCESS] Discovered {len(receipts)} uploaded receipt(s):\n"
                for idx, name in enumerate(receipts, 1):
                    summary += f"  {idx}. {name}\n"
                summary += "\n" + "=" * 60
    
                output_result(result, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Browser query failed: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow D: Browser Draft Report Creation (Headless/Headed)
        # ----------------------------------------------------
        elif args.command == "create-report":
            if args.output == "text":
                print("=" * 60)
                print("     SAP Concur Browser-Based Draft Report Creation")
                print("=" * 60)
            
            headless = not args.headed
            report_name_val = args.name or f"Browser Test Draft {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            purpose = args.purpose or "Validating browser-based creation of draft reports"
            comment = args.comment or "Created automatically via SAP Concur Python Playwright Tester"
    
            try:
                with Spinner(f"Creating report '{report_name_val}'..."):
                    browser_client = ConcurBrowserClient()
                    result = browser_client.create_draft_report(
                        name=report_name_val,
                        purpose=purpose,
                        comment=comment,
                        headless=headless
                    )
                
                summary = "\n[SUCCESS] Browser automation completed successfully!\n"
                summary += f"          Report Created: {result.get('report_name')}\n"
                summary += f"          Screenshots folder: {result.get('screenshot_folder')}\n"
                summary += f"          Notes:          {result.get('notes')}\n" + "=" * 60
                
                output_result(result, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Browser automation failed: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow E: Browser Delete Report
        # ----------------------------------------------------
        elif args.command == "delete-report":
            report_name_val = args.report_name
            if args.output == "text":
                print("=" * 60)
                print(f"     SAP Concur Browser-Based Delete Report: '{report_name_val}'")
                print("=" * 60)
            
            try:
                with Spinner(f"Deleting report '{report_name_val}'..."):
                    browser_client = ConcurBrowserClient()
                    browser_client.delete_report(name=report_name_val, headless=True)
                
                result = {"status": "success", "report_name": report_name_val}
                summary = f"\n[SUCCESS] Successfully deleted report: '{report_name_val}'\n" + "=" * 60
                output_result(result, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to delete report: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow F: Delete All Reports
        # ----------------------------------------------------
        elif args.command == "delete-all-reports":
            if args.output == "text":
                print("=" * 60)
                print("   SAP Concur Browser-Based Delete All Reports")
                print("=" * 60)
            try:
                with Spinner("Deleting all reports..."):
                    browser_client = ConcurBrowserClient()
                    reports = browser_client.list_reports(headless=True)
                    for r in reports:
                        name = r.get("name")
                        browser_client.delete_report(name=name, headless=True)
                
                result = {"status": "success", "count": len(reports)}
                summary = f"\n[SUCCESS] All {len(reports)} reports deleted.\n" + "=" * 60
                output_result(result, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to delete all reports: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow G: Delete All Receipts
        # ----------------------------------------------------
        elif args.command == "delete-all-receipts":
            if args.output == "text":
                print("=" * 60)
                print("   SAP Concur Browser-Based Delete All Receipts")
                print("=" * 60)
            try:
                with Spinner("Deleting all receipts..."):
                    browser_client = ConcurBrowserClient()
                    receipts = browser_client.list_available_receipts(headless=True)
                    for r_name in receipts:
                        browser_client.delete_available_receipt(receipt_name=r_name, headless=True)
                
                result = {"status": "success", "count": len(receipts)}
                summary = f"\n[SUCCESS] All {len(receipts)} available receipts deleted.\n" + "=" * 60
                output_result(result, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to delete all receipts: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow H: Delete All Reports AND Receipts (Nuke)
        # ----------------------------------------------------
        elif args.command == "nuke":
            if args.output == "text":
                print("=" * 60)
                print("   SAP Concur Browser-Based Nuke (Delete All Reports & Receipts)")
                print("=" * 60)
            try:
                with Spinner("Nuking all reports and receipts..."):
                    browser_client = ConcurBrowserClient()
                    reports = browser_client.list_reports(headless=True)
                    for r in reports:
                        browser_client.delete_report(name=r.get("name"), headless=True)
                    receipts = browser_client.list_available_receipts(headless=True)
                    for r_name in receipts:
                        browser_client.delete_available_receipt(receipt_name=r_name, headless=True)
                
                result = {"status": "success", "reports_deleted": len(reports), "receipts_deleted": len(receipts)}
                summary = f"\n[SUCCESS] All {len(reports)} reports and {len(receipts)} receipts deleted.\n" + "=" * 60
                output_result(result, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to delete all items: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow I: Query Historical (Old) Reports
        # ----------------------------------------------------
        elif args.command == "list-old-reports":
            filter_val = args.filter_view
            if args.output == "text":
                print("=" * 60)
                print(f"     SAP Concur Browser-Based Historical Reports (Filter: {filter_val})")
                print("=" * 60)
            try:
                with Spinner(f"Querying historical reports ({filter_val})..."):
                    browser_client = ConcurBrowserClient()
                    reports = browser_client.list_reports(filter_view=filter_val, headless=True)
                
                summary = f"[SUCCESS] Discovered {len(reports)} historical report(s):\n"
                for idx, r in enumerate(reports, 1):
                    summary += f"  {idx}. {r.get('name')} (Purpose: {r.get('purpose', 'None')})\n"
                summary += "=" * 60
                
                output_result(reports, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Historical reports query failed: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow J: Report Details of a Report
        # ----------------------------------------------------
        elif args.command == "report-details":
            report_name_val = args.report_name
            filter_val = args.filter_view
            if args.output == "text":
                print("=" * 60)
                print(f"     SAP Concur Report Details: '{report_name_val}'")
                print("=" * 60)
            try:
                with Spinner(f"Fetching details for '{report_name_val}'..."):
                    browser_client = ConcurBrowserClient()
                    details = browser_client.get_report_details(name=report_name_val, filter_view=filter_val, deep=args.deep, headless=True)
                
                summary = "[SUCCESS] Details retrieved:\n"
                summary += f"  Name:     {details.get('report_name')}\n"
                summary += f"  Number:   {details.get('report_number')}\n"
                summary += f"  Purpose:  {details.get('purpose')}\n"
                summary += f"  Comment:  {details.get('comment')}\n"
                summary += f"  Expenses: ({len(details.get('expenses'))} items)\n"
                for item in details.get('expenses'):
                    summary += f"    - {item.get('raw_text')}\n"
                    if item.get('type') and item.get('type') != 'Unknown':
                        summary += f"      Type:             {item.get('type')}\n"
                    if item.get('business_purpose'):
                        summary += f"      Business Purpose: {item.get('business_purpose')}\n"
                    if item.get('comment'):
                        summary += f"      Comment:          {item.get('comment')}\n"
                summary += "=" * 60
                
                output_result(details, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to get report details: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
     
        # ----------------------------------------------------
        # Flow J2: Report Allocations
        # ----------------------------------------------------
        elif args.command == "allocations":
            try:
                with Spinner(f"Fetching allocations for '{args.report_name}'..."):
                    browser_client = ConcurBrowserClient()
                    data = browser_client.get_report_allocations(args.report_name, filter_view=args.filter_view, headless=True)
                    print(json.dumps(data, indent=2))
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                print(json.dumps({"status": "error", "message": str(e)}))
    
        # ----------------------------------------------------
        # Flow J3: Add Allocation
        # ----------------------------------------------------
        elif args.command == "detach-receipt":
            try:
                with Spinner(f"Detaching receipt from index {args.index} in "
                             f"'{args.report_name}'..."):
                    browser_client = ConcurBrowserClient()
                    res = browser_client.detach_transaction_receipt(
                        report_name=args.report_name,
                        transaction_index=args.index,
                        headless=True,
                    )
                if res.get("success"):
                    summary = (f"\n[SUCCESS] Receipt detached from index {args.index}.\n"
                               + "=" * 60)
                else:
                    summary = (f"\n[FAILED] Could not detach receipt from index "
                               f"{args.index}: {res.get('error')}\n" + "=" * 60)
                output_result(res, summary)
            except ConcurSessionExpiredError as e:
                handle_session_expired(e)
            except Exception as e:
                print(json.dumps({"status": "error", "message": str(e)}))

        elif args.command == "unallocate":
            try:
                with Spinner(f"Clearing allocations on index {args.index} in "
                             f"'{args.report_name}'..."):
                    browser_client = ConcurBrowserClient()
                    res = browser_client.remove_transaction_allocations(
                        report_name=args.report_name,
                        transaction_index=args.index - 1,  # Convert to 0-based
                        headless=True,
                    )
                if res.get("success"):
                    summary = (f"\n[SUCCESS] Cleared {res.get('removed', 0)} allocation(s) "
                               f"on index {args.index}.\n" + "=" * 60)
                else:
                    summary = (f"\n[FAILED] Could not clear allocations on index "
                               f"{args.index}: {res.get('error')}\n" + "=" * 60)
                output_result(res, summary)
            except ConcurSessionExpiredError as e:
                handle_session_expired(e)
            except Exception as e:
                print(json.dumps({"status": "error", "message": str(e)}))

        elif args.command == "add-allocation":
            try:
                with Spinner(f"Adding allocation to index {args.index} in '{args.report_name}'..."):
                    browser_client = ConcurBrowserClient()
                    res = browser_client.add_transaction_allocation(
                        report_name=args.report_name,
                        transaction_index=args.index - 1, # Convert to 0-based
                        department=args.dept,
                        fund=args.fund,
                        program=args.prog,
                        headless=True
                    )
                
                if res.get("success"):
                    summary = f"\n[SUCCESS] Allocation added to transaction {args.index} in '{args.report_name}'!\n"
                    summary += f"  - Dept: {args.dept}\n"
                    summary += f"  - Fund: {args.fund}\n"
                    if args.prog:
                        summary += f"  - Prog: {args.prog}\n"
                    summary += "=" * 60
                    output_result(res, summary)
                else:
                    output_result(res)
            except ConcurSessionExpiredError as e:
                handle_session_expired(e)
            except Exception as e:
                print(json.dumps({"status": "error", "message": str(e)}))
    
        # ----------------------------------------------------
        # Flow K: List Card Transactions
        # ----------------------------------------------------
        elif args.command == "list-cards":
            filter_val = args.filter_view
            if args.output == "text":
                print("=" * 60)
                print(f"     SAP Concur Card Transactions (Filter: {filter_val})")
                print("=" * 60)
            try:
                with Spinner(f"Querying card transactions ({filter_val})..."):
                    browser_client = ConcurBrowserClient()
                    txs = browser_client.list_card_transactions(card_type_filter=filter_val, headless=True)
                
                summary = f"[SUCCESS] Discovered {len(txs)} transaction(s):\n"
                for idx, t in enumerate(txs, 1):
                    summary += f"  {idx}. {t.get('raw_text')}\n"
                summary += "=" * 60
                
                output_result(txs, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Listing card transactions failed: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow L: Get Card Transaction Details
        # ----------------------------------------------------
        elif args.command == "card-details":
            tx_id = args.merchant_or_id
            filter_val = args.filter_view
            if args.output == "text":
                print("=" * 60)
                print(f"     SAP Concur Card Transaction Details: '{tx_id}'")
                print("=" * 60)
            try:
                with Spinner(f"Fetching transaction details for '{tx_id}'..."):
                    browser_client = ConcurBrowserClient()
                    details = browser_client.get_card_transaction_details(merchant_or_id=tx_id, card_type_filter=filter_val, headless=True)
                
                summary = "[SUCCESS] Transaction details:\n"
                summary += f"  Merchant:     {details.get('merchant')}\n"
                summary += f"  Date:         {details.get('date')}\n"
                summary += f"  Amount:       {details.get('amount')}\n"
                summary += f"  ID:           {details.get('transaction_id')}\n"
                summary += f"  Card Program: {details.get('card_program')}\n"
                summary += "=" * 60
                
                output_result(details, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to get transaction details: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow M: Add Delegate
        # ----------------------------------------------------
        elif args.command == "add-delegate":
            name = args.name_or_email
            perms = args.delegate_perms
            if args.output == "text":
                print("=" * 60)
                print(f"     SAP Concur Add Expense Delegate: '{name}'")
                print(f"     Permissions: {perms}")
                print("=" * 60)
            try:
                with Spinner(f"Adding delegate '{name}'..."):
                    browser_client = ConcurBrowserClient()
                    browser_client.add_expense_delegate(name_or_email=name, permissions=perms, headless=True)
                
                result = {"status": "success", "name": name, "permissions": perms}
                summary = f"\n[SUCCESS] Delegate '{name}' added successfully!\n" + "=" * 60
                output_result(result, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to add delegate: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow N: Remove Delegate
        # ----------------------------------------------------
        elif args.command == "remove-delegate":
            name = args.name_or_email
            if args.output == "text":
                print("=" * 60)
                print(f"     SAP Concur Remove Expense Delegate: '{name}'")
                print("=" * 60)
            try:
                with Spinner(f"Removing delegate '{name}'..."):
                    browser_client = ConcurBrowserClient()
                    browser_client.remove_expense_delegate(name_or_email=name, headless=True)
                
                result = {"status": "success", "name": name}
                summary = f"\n[SUCCESS] Delegate '{name}' removed successfully!\n" + "=" * 60
                output_result(result, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to remove delegate: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow O: Reconcile Report Transactions
        # ----------------------------------------------------
        elif args.command == "reconcile":
            report_name_val = args.report_name
            rules_path = args.reconcile_rules
            
            reconciliation_rules = {
                "Uber": {
                    "expense_type": "Ground Transportation",
                    "business_purpose": "Client dinner ride",
                    "comment": "Uber Ride",
                    "allocation_code": "COST-01"
                },
                "Office Depot": {
                    "expense_type": "Office Supplies",
                    "business_purpose": "Team materials",
                    "comment": "Pens and notebooks",
                    "allocation_code": "COST-02"
                }
            }
            
            if rules_path:
                try:
                    with open(rules_path, "r") as f:
                        reconciliation_rules = json.load(f)
                except ConcurSessionExpiredError as e:

                    handle_session_expired(e)

                except Exception as e:
                    if args.output == "text":
                        print(f"[ERROR] Failed to load reconciliation rules JSON from '{rules_path}': {str(e)}")
                    else:
                        print(json.dumps({"status": "error", "message": f"Failed to load rules: {str(e)}"}))
                    sys.exit(1)
                    
            if args.output == "text":
                print("=" * 60)
                print(f"     SAP Concur Report Reconciliation: '{report_name_val}'")
                print("=" * 60)
            try:
                with Spinner(f"Reconciling report '{report_name_val}'..."):
                    browser_client = ConcurBrowserClient()
                    res = browser_client.reconcile_report(
                        report_name=report_name_val,
                        reconciliation_rules=reconciliation_rules,
                        headless=True,
                        submit=args.submit
                    )
                
                if args.submit:
                    summary = f"\n[SUCCESS] Report '{report_name_val}' reconciled and submitted successfully!\n" + "=" * 60
                else:
                    summary = f"\n[SUCCESS] Report '{report_name_val}' reconciled successfully! (Draft mode, not submitted)\n" + "=" * 60
                
                output_result(res, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Reconciliation failed: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    
        # ----------------------------------------------------
        # Flow P: Attach Receipt to Transaction
        # ----------------------------------------------------
        elif args.command == "attach-receipt":
            report_name_val = args.report_name
            merchant = args.merchant
            receipt_path = args.receipt_path
    
            if args.output == "text":
                print("=" * 60)
                print(f"     SAP Concur Attach Receipt: '{receipt_path}' to '{merchant}' in '{report_name_val}'")
                print("=" * 60)
            try:
                with Spinner(f"Attaching receipt to '{merchant}'..."):
                    browser_client = ConcurBrowserClient()
                    browser_client.attach_receipt_to_transaction(
                        report_name=report_name_val,
                        merchant_or_id=merchant,
                        receipt_file_path=receipt_path,
                        headless=True
                    )
                
                result = {"status": "success", "merchant": merchant, "receipt": receipt_path}
                summary = f"\n[SUCCESS] Receipt '{receipt_path}' attached successfully!\n" + "=" * 60
                output_result(result, summary)
            except ConcurSessionExpiredError as e:

                handle_session_expired(e)

            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to attach receipt: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)

        # ----------------------------------------------------
        # Flow Q: Update Transaction Fields
        # ----------------------------------------------------
        elif args.command == "update-transaction":
            report_name_val = args.report_name
            tx_indices = args.transaction_indices
            exp_type = args.type
            bus_purpose = args.purpose
            cmt = args.comment
            
            if args.justification:
                if not bus_purpose: bus_purpose = args.justification
                if not cmt: cmt = args.justification

            if args.output == "text":
                print("=" * 60)
                print(f"     SAP Concur Update Transaction: {len(tx_indices)} items in '{report_name_val}'")
                print("=" * 60)
            try:
                with Spinner(f"Updating {len(tx_indices)} transaction(s) in report '{report_name_val}'..."):
                    browser_client = ConcurBrowserClient()
                    res = browser_client.update_report_transaction(
                        report_name=report_name_val,
                        transaction_indices=tx_indices,
                        expense_type=exp_type,
                        business_purpose=bus_purpose,
                        comment=cmt,
                        headless=True
                    )
                
                success_count = sum(1 for r in res.get("results", []) if r["success"])
                summary = f"\n[SUCCESS] Updated {success_count}/{len(tx_indices)} transactions successfully!\n"
                for r in res.get("results", []):
                    status = "OK" if r["success"] else f"FAILED ({r.get('error')})"
                    if r.get("validation_error"):
                        status += f" (WARNING: {r['validation_error']})"
                    elif r.get("note"):
                        status += f" (NOTE: {r['note']})"
                    summary += f"  - Index {r['index']}: {status}\n"
                summary += "=" * 60
                output_result(res, summary)
            except ConcurSessionExpiredError as e:
                handle_session_expired(e)
            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to update transaction: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
        # ----------------------------------------------------
        # Flow R: Update Report Header Fields
        # ----------------------------------------------------
        elif args.command == "update-report":
            old_name = args.report_name
            new_name = args.name or old_name
            bus_purpose = args.purpose
            cmt = args.comment
            
            if args.justification:
                if not bus_purpose: bus_purpose = args.justification
                if not cmt: cmt = args.justification

            if args.output == "text":
                print("=" * 60)
                print(f"     SAP Concur Update Report Header: '{old_name}'")
                print("=" * 60)
            try:
                with Spinner(f"Updating report header for '{old_name}'..."):
                    browser_client = ConcurBrowserClient()
                    res = browser_client.update_report(
                        old_name=old_name,
                        new_name=new_name,
                        new_purpose=bus_purpose,
                        new_comment=cmt,
                        headless=True
                    )
                
                summary = f"\n[SUCCESS] Successfully updated report header for '{old_name}'!\n"
                if new_name != old_name:
                    summary += f"  - New Name: {new_name}\n"
                if bus_purpose:
                    summary += f"  - Purpose:  {bus_purpose}\n"
                if cmt:
                    summary += f"  - Comment:  {cmt}\n"
                summary += "=" * 60
                output_result(res, summary)
            except ConcurSessionExpiredError as e:
                handle_session_expired(e)
            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to update report header: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)

        # ----------------------------------------------------
        # Flow S: Submit Report
        # ----------------------------------------------------
        elif args.command == "submit-report":
            report_name_val = args.report_name
            if args.output == "text":
                print("=" * 60)
                print(f"     SAP Concur Submit Report: '{report_name_val}'")
                print("=" * 60)
            try:
                with Spinner(f"Submitting report '{report_name_val}'..."):
                    browser_client = ConcurBrowserClient()
                    res = browser_client.submit_report(report_name=report_name_val, headless=True)
                
                # submit_report returns success=False when it clicked but could
                # not verify the report left draft. Printing [SUCCESS] for that
                # is how an unsubmitted report gets reported as submitted.
                if res.get("success"):
                    summary = f"\n[SUCCESS] Successfully submitted report: '{report_name_val}'\n"
                else:
                    summary = (f"\n[WARNING] Submit was clicked but could not be verified for "
                               f"'{report_name_val}'. Check the report in Concur.\n")
                summary += "=" * 60
                output_result(res, summary)
                if not res.get("success"):
                    sys.exit(1)
            except ConcurSessionExpiredError as e:
                handle_session_expired(e)
            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to submit report: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)

        # ----------------------------------------------------
        # Flow T: Apply JSON Updates
        # ----------------------------------------------------
        elif args.command == "apply-json":
            json_path = args.json_path
            if args.output == "text":
                print("=" * 60)
                print(f"     SAP Concur Apply JSON Updates: '{json_path}'")
                print("=" * 60)
            try:
                if not os.path.exists(json_path):
                    raise FileNotFoundError(f"JSON file not found: {json_path}")
                with open(json_path, 'r') as f:
                    data = json.load(f)
                
                report_name_val = data.get("report_name")
                expenses = data.get("expenses", [])

                if not report_name_val:
                    raise KeyError("Missing 'report_name' in JSON file.")

                if not args.include_incomplete:
                    expenses, withheld = split_incomplete_expenses(expenses, data)
                    for e in withheld:
                        print(
                            f"[SKIP] index {e.get('index')} ({e.get('vendor')} "
                            f"{e.get('amount')}): captured incompletely "
                            f"(deep scan failed), so its fields are not safe to write. "
                            f"Re-run `report show --deep` or pass --include-incomplete.",
                            file=sys.stderr,
                        )
                
                if args.output == "text":
                    print(f"[*] Report Name: {report_name_val}")
                    print(f"[*] Transactions: {len(expenses)}")
                    print("-" * 60)
                    
                headless = not args.headed
                mode = "headlessly" if headless else "in a visible browser"
                with Spinner(f"Applying JSON updates to report '{report_name_val}' {mode}..."):
                    browser_client = ConcurBrowserClient()
                    res = browser_client.apply_json_updates(
                        report_name=report_name_val, expenses=expenses,
                        headless=headless,
                        ignore_session_budget=args.ignore_session_budget)
                
                summary = f"\n[SUCCESS] Custom JSON updates successfully applied to Concur!\n"
                summary += "=" * 60
                output_result(res, summary)
            except ConcurSessionExpiredError as e:
                handle_session_expired(e)
            except Exception as e:
                if args.output == "text":
                    print(f"\n[ERROR] Failed to apply JSON updates: {str(e)}\n" + "=" * 60)
                else:
                    print(json.dumps({"status": "error", "message": str(e)}))
                sys.exit(1)
    except ConcurSessionExpiredError as e:
        handle_session_expired(e)


def main():
    run_tests()


if __name__ == "__main__":
    main()
