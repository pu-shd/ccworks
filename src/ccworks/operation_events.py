"""Small, allowlisted operation events and private atomic progress checkpoints."""

from datetime import datetime, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import time
from typing import Any

from ccworks import paths
from ccworks.safari_login import save_state_atomically

logger = logging.getLogger("ccworks.events")


class RedactedBrowserDiagnostics(logging.Filter):
    """Legacy free-text diagnostics are not a safe channel for expense data."""

    def filter(self, record):
        if record.levelno < logging.WARNING:
            return False
        record.msg = json.dumps(
            {
                "event": "browser_diagnostic",
                "level": record.levelname,
                "line": record.lineno,
            },
            sort_keys=True,
        )
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


OPERATIONS = {"submission", "reconciliation", "allocation", "browser"}
PHASES = {
    "started",
    "preflight",
    "summary",
    "certification",
    "confirmation",
    "fields",
    "receipt",
    "allocation",
    "verification",
    "completed",
    "overlay",
    "session",
    "checkpoint",
}
OUTCOMES = {
    "started",
    "attempted",
    "acknowledged",
    "verified",
    "skipped",
    "not_submitted",
    "unknown",
    "failed",
    "handled",
}


class OperationEvents:
    """Never accept report names, expense text, receipt paths or exception messages."""

    def __init__(self, operation: str):
        if operation not in OPERATIONS:
            raise ValueError("Unsupported operation event")
        self.operation = operation
        self.started = time.monotonic()
        self.events: list[dict[str, Any]] = []

    def emit(
        self,
        phase: str,
        outcome: str,
        *,
        index: int | None = None,
        attempt: int = 1,
        session_minutes: float | None = None,
    ):
        if phase not in PHASES or outcome not in OUTCOMES:
            raise ValueError("Unsupported operation phase or outcome")
        if index is not None and (type(index) is not int or index < 1):
            raise ValueError("Invalid operation index")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("Invalid operation attempt")
        if session_minutes is not None:
            if type(session_minutes) not in (int, float) or not math.isfinite(
                session_minutes
            ):
                raise ValueError("Invalid session budget")
        event = {
            "operation": self.operation,
            "phase": phase,
            "outcome": outcome,
            "attempt": attempt,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
        }
        if index is not None:
            event["index"] = index
        if session_minutes is not None:
            event["session_minutes"] = round(session_minutes, 1)
        self.events.append(event)
        logger.info(json.dumps(event, sort_keys=True))
        return event


class ProgressCheckpoint:
    """A checkpoint is evidence of an attempt, never authority to skip readback."""

    def __init__(self, operation: str, identity: str, directory: Path | None = None):
        if operation not in OPERATIONS:
            raise ValueError("Unsupported checkpoint operation")
        key = hashlib.sha256(identity.encode()).hexdigest()
        self.path = (
            (directory or paths.state_dir()) / "operations" / f"{operation}-{key}.json"
        )
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.operation = operation

    def write(self, phase: str, outcome: str, *, attempted: bool = False):
        if (
            phase not in PHASES
            or outcome not in OUTCOMES
            or type(attempted) is not bool
        ):
            raise ValueError("Unsupported checkpoint state")
        save_state_atomically(
            self.path,
            {
                "schema_version": 1,
                "operation": self.operation,
                "phase": phase,
                "outcome": outcome,
                "attempted": attempted,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
