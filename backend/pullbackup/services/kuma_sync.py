# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""Tell Uptime Kuma what the freshness projection says, and nothing else.

A push monitor is a dead-man switch. The old code pushed `up` on every
successful run and `down` on every failed one, with an expiry equal to the
cadence. Two defects follow: a normal run that takes a little longer than the
cadence lets the monitor lapse (flapping), and a single failed attempt pushed
`down` even though the previous success was still well inside its promised
window, while a healthy-looking retry could never express "stale".

Now every push is derived from `freshness.project_task` -- the same function
the API and the UI use -- and from nothing else:

    fresh  -> push `up`    (the newest SUCCESS covers every occurrence due)
    stale  -> push `down`  (a promised backup is overdue past its bounded grace)
    other  -> push nothing (not yet due / delayed within grace / disabled)

There is no periodic unconditional `up`: a sweep only ever repeats what the
projection currently says. Failed or cancelled attempts and a healthy
scheduler change the REASON text, never the status. The monitor's own expiry
(`freshness.kuma_expiry_seconds`) stays as the backstop for a process that is
down altogether.

Delivery failures are bounded, retried, recorded and visible
(`failures()`); a failed push is never reported as a successful sync.
Error text carries the exception class or HTTP status only: the push URL holds
the monitor token and must never reach a log or an API response.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Optional

import httpx
from sqlmodel import Session, select

from ..config import settings
from ..models import Task, utcnow
from . import freshness

log = logging.getLogger(__name__)

PUSH_ATTEMPTS = 3
PUSH_RETRY_DELAYS = (1.0, 3.0)  # seconds slept before attempt 2 and 3
SWEEP_INTERVAL_SECONDS = 300
SWEEP_JOB_ID = "kuma-freshness-sweep"

Push = Callable[..., Awaitable["PushResult"]]


@dataclass(frozen=True)
class PushResult:
    ok: bool
    attempts: int
    error: Optional[str] = None


@dataclass(frozen=True)
class SyncOutcome:
    task_id: int
    state: str
    reason: str
    status: Optional[str]      # what the projection asked Kuma to be told
    pushed: bool               # a push was attempted
    ok: bool                   # nothing to push, or the push was accepted
    attempts: int = 0
    error: Optional[str] = None
    at: Optional[datetime] = None


# task_id -> newest outcome. In memory: it exists to make delivery failure
# visible while it is happening; the next sweep rebuilds it after a restart.
_outcomes: dict[int, SyncOutcome] = {}
# task_id -> error text for failed monitor CONFIGURATION (create/edit/delete).
_sync_failures: dict[int, str] = {}
# task_id -> operations ("create"|"interval"|"active"|"delete") still owed.
_pending_ops: dict[int, tuple[str, ...]] = {}


def _engine():
    from .. import db

    return db.engine


def safe_error(error: BaseException) -> str:
    """Class name or HTTP status only. Never the message: it can hold the URL."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


def _retryable(error: BaseException) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code >= 500
    return True


async def push_heartbeat(
    token: str,
    status: str,
    msg: str = "",
    *,
    client: Optional[httpx.AsyncClient] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PushResult:
    """GET the push URL, at most PUSH_ATTEMPTS times. 4xx is not retried."""
    if not settings.kuma_enabled or not token:
        return PushResult(False, 0, "kuma_not_configured")
    url = f"{settings.uptime_kuma_url.rstrip('/')}/api/push/{token}"
    owned = client is None
    http = client or httpx.AsyncClient(timeout=10)
    last = "unknown"
    attempts = 0
    try:
        for attempt in range(PUSH_ATTEMPTS):
            if attempt:
                await sleep(PUSH_RETRY_DELAYS[min(attempt - 1, len(PUSH_RETRY_DELAYS) - 1)])
            attempts += 1
            try:
                r = await http.get(url, params={"status": status, "msg": msg, "ping": ""})
                r.raise_for_status()
                return PushResult(True, attempts)
            except Exception as e:  # noqa: BLE001 - classified by safe_error
                last = safe_error(e)
                if not _retryable(e):
                    break
    finally:
        if owned:
            await http.aclose()
    log.warning("kuma push failed after %d attempt(s): %s", attempts, last)
    return PushResult(False, attempts, last)


def message_for(f: freshness.Freshness) -> str:
    """Operator-facing push text. Identity of the last success, no secrets."""
    when = "never" if f.last_success_at is None else f.last_success_at.isoformat()
    run = "-" if f.last_success_run_id is None else f.last_success_run_id
    return f"{f.state}: {f.reason}; last success run {run} finished {when}"


async def sync_task(
    task_id: int,
    *,
    push: Optional[Push] = None,
    now: Optional[datetime] = None,
) -> Optional[SyncOutcome]:
    """Project one task and push what the projection says. None: nothing to sync."""
    push = push or push_heartbeat
    with Session(_engine()) as session:
        task = session.get(Task, task_id)
        if task is None or not task.kuma_enabled or not task.kuma_push_token:
            return None
        f = freshness.project_task(session, task, now=now)
        token = task.kuma_push_token
    at = utcnow()
    if f.kuma_status is None:
        outcome = SyncOutcome(task_id, f.state, f.reason, None, False, True, at=at)
    else:
        result = await push(token, f.kuma_status, message_for(f))
        # Tests and callers may hand back a bare truthy value; only a PushResult
        # with ok=True is a delivered push.
        ok = bool(getattr(result, "ok", False))
        outcome = SyncOutcome(
            task_id, f.state, f.reason, f.kuma_status, True, ok,
            getattr(result, "attempts", 0), getattr(result, "error", None), at,
        )
    _outcomes[task_id] = outcome
    return outcome


async def sweep(*, push: Optional[Push] = None, now: Optional[datetime] = None) -> list[SyncOutcome]:
    """Re-assert the projection for every monitored task (bounded, no unconditional up)."""
    with Session(_engine()) as session:
        ids = [
            t.id
            for t in session.exec(select(Task).where(Task.kuma_enabled == True)).all()  # noqa: E712
            if t.kuma_push_token
        ]
    out = []
    for task_id in ids:
        try:
            outcome = await sync_task(task_id, push=push, now=now)
        except Exception as e:  # noqa: BLE001 - one task never stops the sweep
            log.warning("kuma sweep failed for task %s: %s", task_id, safe_error(e))
            outcome = SyncOutcome(task_id, "unknown", "sweep_error", None, False, False, error=safe_error(e), at=utcnow())
            _outcomes[task_id] = outcome
        if outcome is not None:
            out.append(outcome)
    return out


async def sweep_job() -> None:
    """Scheduler entry. Separate job from the liveness heartbeat on purpose."""
    try:
        await sweep()
    except Exception:
        log.exception("kuma freshness sweep failed")


def record_sync_failure(task_id: int, error: BaseException, pending: tuple[str, ...] = ()) -> None:
    """Record a failed configuration sync together with the operations that
    did NOT complete (the failed one and any later one it blocked), so a later
    sync retries exactly those and clears the failure only when they succeed."""
    _sync_failures[task_id] = safe_error(error)
    _pending_ops[task_id] = tuple(pending)


def pending_ops(task_id: int) -> tuple[str, ...]:
    return _pending_ops.get(task_id, ())


def clear_sync_failure(task_id: int) -> None:
    _sync_failures.pop(task_id, None)
    _pending_ops.pop(task_id, None)


def failures() -> list[dict]:
    """Everything currently failing to reach Kuma, for the operator API."""
    rows = [
        {"task_id": o.task_id, "kind": "push", "error": o.error, "attempts": o.attempts,
         "at": None if o.at is None else o.at.isoformat()}
        for o in _outcomes.values()
        if not o.ok
    ]
    rows += [{"task_id": t, "kind": "configuration", "error": e, "attempts": 1, "at": None}
             for t, e in _sync_failures.items()]
    return sorted(rows, key=lambda r: (r["task_id"], r["kind"]))
