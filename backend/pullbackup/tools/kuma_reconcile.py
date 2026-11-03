# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""Dry-run-first reconciliation of existing Kuma push monitors.

Existing monitors carry an expiry equal to the cron cadence. This lines each
one up with `freshness.kuma_expiry_seconds` (the dead-man backstop; the
projection pushed by `kuma_sync` is what says DOWN).

What it touches, exactly: the `interval` of monitors already linked to a task.
It re-submits the monitor Kuma returned with only `interval` changed, so the
monitor id, push token, parent group, notification list and the operator's
active/paused state ride through untouched. It never calls pause/resume, never
creates or deletes a monitor, and never pushes a heartbeat.

`plan()` is read-only. `apply()` is the only mutating entry point and is
bounded: each monitor gets RETRY_ATTEMPTS tries, a failure is recorded in the
report (error CLASS only - never a URL or token), and `Report.ok` is False
unless every planned change was accepted. Run it against a live Kuma only as a
separately approved operational step; the tests use stubs.

    python -m pullbackup.tools.kuma_reconcile            # dry run (default)
    python -m pullbackup.tools.kuma_reconcile --apply    # mutate
"""
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Optional, Protocol

from sqlmodel import Session, select

from ..models import Task
from ..services import freshness, kuma_sync

RETRY_ATTEMPTS = 3

OK = "ok"                      # already at the desired interval
UPDATE = "update"              # would change (plan) / was changed (apply)
UPDATED = "updated"
FAILED = "failed"
SKIPPED = "skipped"            # linked task has no monitor id to reconcile


class MonitorAPI(Protocol):
    async def get_monitor(self, monitor_id: int) -> dict: ...
    async def update_interval(self, monitor_id: int, interval_s: int) -> None: ...


@dataclass
class Item:
    task_id: int
    monitor_id: Optional[int]
    action: str
    current_interval: Optional[int] = None
    desired_interval: Optional[int] = None
    monitor_active: Optional[bool] = None  # observed, reported, never changed
    attempts: int = 0
    error: Optional[str] = None


@dataclass
class Report:
    applied: bool
    items: list[Item] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(i.action != FAILED for i in self.items)

    def to_json(self) -> str:
        return json.dumps({"applied": self.applied, "ok": self.ok, "items": [asdict(i) for i in self.items]}, indent=2)


def _targets(session: Session) -> list[Task]:
    return [
        t
        for t in session.exec(select(Task).where(Task.kuma_enabled == True)).all()  # noqa: E712
        if t.kuma_monitor_id
    ]


async def plan(session: Session, api: MonitorAPI) -> Report:
    """Read-only: compare every linked monitor with its desired expiry."""
    report = Report(applied=False)
    for task in _targets(session):
        desired = freshness.kuma_expiry_seconds(task.cron)
        item = Item(task.id, task.kuma_monitor_id, OK, desired_interval=desired)
        try:
            mon = await api.get_monitor(task.kuma_monitor_id)
            item.current_interval = int(mon.get("interval") or 0)
            item.monitor_active = mon.get("active")
            if item.current_interval != max(desired, 60):
                item.action = UPDATE
        except Exception as e:  # noqa: BLE001
            item.action, item.error = FAILED, kuma_sync.safe_error(e)
        report.items.append(item)
    return report


async def apply(session: Session, api: MonitorAPI, *, sleep=asyncio.sleep) -> Report:
    """Make every planned change, bounded. Never pauses/resumes, never deletes."""
    report = await plan(session, api)
    report.applied = True
    for item in report.items:
        if item.action != UPDATE:
            continue
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            item.attempts = attempt
            try:
                await api.update_interval(item.monitor_id, item.desired_interval)
                item.action, item.error = UPDATED, None
                break
            except Exception as e:  # noqa: BLE001
                item.action, item.error = FAILED, kuma_sync.safe_error(e)
                if attempt < RETRY_ATTEMPTS:
                    await sleep(2.0 * attempt)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    from .. import db
    from ..services import kuma

    args = sys.argv[1:] if argv is None else argv
    k = kuma.client()
    if k is None:
        print(json.dumps({"ok": False, "error": "kuma_not_configured"}))
        return 2
    run = apply if "--apply" in args else plan

    async def go() -> Report:
        with Session(db.engine) as session:
            return await run(session, k)

    report = asyncio.run(go())
    print(report.to_json())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
