# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""What Kuma is told, how delivery failure is surfaced, reconciliation, and
API/Kuma projection parity. HTTP is stubbed; nothing here touches a network
or a real token (tokens are obvious fixtures).
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pullbackup.api import system as system_api
from pullbackup.api import tasks as tasks_api
from pullbackup.models import Run, RunState, Task, TaskDue
from pullbackup.services import due, freshness, kuma_sync
from pullbackup.tools import kuma_reconcile
from sqlmodel import Session

from test_due_work import clock, make_source, make_task, sqlite_engine  # noqa: F401

UTC = timezone.utc
H = 3600
TOKEN = "fixture-token-not-a-secret"


def at(*a):
    return datetime(*a, tzinfo=UTC)


@pytest.fixture(autouse=True)
def env(clock, monkeypatch):
    # Depends on `clock` so this runs AFTER it: the clock fixture pins TZ=UTC.
    monkeypatch.setenv("TZ", "America/Denver")
    monkeypatch.setattr(kuma_sync, "_outcomes", {})
    monkeypatch.setattr(kuma_sync, "_sync_failures", {})
    s = kuma_sync.settings
    monkeypatch.setattr(s, "uptime_kuma_url", "http://kuma.invalid")
    monkeypatch.setattr(s, "uptime_kuma_username", "u")
    monkeypatch.setattr(s, "uptime_kuma_password", "p")


class Recorder:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    async def __call__(self, token, status, msg=""):
        self.calls.append((token, status, msg))
        return kuma_sync.PushResult(self.ok, 1, None if self.ok else "ConnectError")


def add_task(engine, tmp_path, now, *, enabled=True, kuma=True, cron="0 3 * * *", source="source", **kw):
    src = make_source(engine, source)
    tid = make_task(engine, tmp_path, src, 0, now, cron=cron, enabled=enabled,
                    kuma_enabled=kuma, kuma_monitor_id=9 if kuma else None,
                    kuma_push_token=TOKEN if kuma else None, **kw)
    with Session(engine) as s:
        t = s.get(Task, tid)
        t.created_at = at(2026, 5, 1)
        s.add(t)
        s.commit()
    return tid


def add_run(engine, tid, state, started, seconds=H):
    with Session(engine) as s:
        done = state not in (RunState.pending, RunState.running)
        r = Run(task_id=tid, state=state, started_at=started,
                finished_at=started + timedelta(seconds=seconds) if done else None)
        s.add(r)
        s.commit()
        return r.id


LAST = at(2026, 6, 1, 9, 0, 30)
STALE_NOW = at(2026, 6, 3, 9) + timedelta(seconds=freshness.grace_seconds(86400, (H,)) + 60)


# ------------------------------------------------------------ what is pushed


@pytest.mark.asyncio
async def test_stale_pushes_down_and_repeated_sweeps_never_turn_it_up(sqlite_engine, tmp_path, clock):
    tid = add_task(sqlite_engine, tmp_path, STALE_NOW)
    add_run(sqlite_engine, tid, RunState.success, LAST)
    rec = Recorder()
    for _ in range(3):
        await kuma_sync.sweep(push=rec, now=STALE_NOW)
    assert [c[1] for c in rec.calls] == ["down", "down", "down"]
    assert all(TOKEN == c[0] for c in rec.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("latest", [RunState.failed, RunState.cancelled])
async def test_failed_or_cancelled_attempts_never_push_up_for_a_stale_backup(sqlite_engine, tmp_path, clock, latest):
    tid = add_task(sqlite_engine, tmp_path, STALE_NOW)
    add_run(sqlite_engine, tid, RunState.success, LAST)
    for i in range(4):
        add_run(sqlite_engine, tid, latest, at(2026, 6, 3, 9, 5 + i), seconds=60)
    with Session(sqlite_engine) as s:
        s.add(TaskDue(task_id=tid, due_at=at(2026, 6, 2, 9), status=due.RETRY, attempts=4))
        s.commit()
    rec = Recorder()
    out = await kuma_sync.sweep(push=rec, now=STALE_NOW)
    assert [c[1] for c in rec.calls] == ["down"]
    assert out[0].state == "stale"


@pytest.mark.asyncio
async def test_fresh_pushes_up_only_because_a_success_covers_the_occurrence(sqlite_engine, tmp_path, clock):
    tid = add_task(sqlite_engine, tmp_path, STALE_NOW)
    add_run(sqlite_engine, tid, RunState.success, at(2026, 6, 3, 9, 0, 30))
    rec = Recorder()
    await kuma_sync.sweep(push=rec, now=at(2026, 6, 3, 20))
    assert [c[1] for c in rec.calls] == ["up"]
    assert "run" in rec.calls[0][2] and TOKEN not in rec.calls[0][2]


@pytest.mark.asyncio
async def test_a_healthy_scheduler_alone_pushes_nothing(sqlite_engine, tmp_path, clock):
    """No history and not yet due: the sweep stays silent, it does not invent an up."""
    now = at(2026, 6, 1, 13)
    tid = add_task(sqlite_engine, tmp_path, now)
    with Session(sqlite_engine) as s:
        t = s.get(Task, tid)
        t.created_at = at(2026, 6, 1, 12)
        s.add(t)
        s.commit()
    rec = Recorder()
    out = await kuma_sync.sweep(push=rec, now=now)
    assert rec.calls == []
    assert (out[0].state, out[0].reason, out[0].pushed, out[0].ok) == ("not_yet_due", "no_history", False, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["disabled", "no_monitor", "kuma_off", "no_token"])
async def test_disabled_unmonitored_tasks_are_never_pushed(sqlite_engine, tmp_path, clock, shape):
    tid = add_task(
        sqlite_engine, tmp_path, STALE_NOW,
        enabled=shape != "disabled", kuma=shape != "kuma_off",
    )
    if shape == "no_token":
        with Session(sqlite_engine) as s:
            t = s.get(Task, tid)
            t.kuma_push_token = None
            s.add(t)
            s.commit()
    if shape == "no_monitor":
        with Session(sqlite_engine) as s:
            t = s.get(Task, tid)
            t.kuma_enabled = False
            s.add(t)
            s.commit()
    add_run(sqlite_engine, tid, RunState.success, LAST)
    rec = Recorder()
    await kuma_sync.sweep(push=rec, now=STALE_NOW)
    assert rec.calls == []


@pytest.mark.asyncio
async def test_notify_dispatch_pushes_the_projection_not_the_run_outcome(sqlite_engine, tmp_path, clock, monkeypatch):
    """A successful run whose success is somehow not covering still follows the projection."""
    from pullbackup.services import notify

    clock.now = STALE_NOW
    tid = add_task(sqlite_engine, tmp_path, STALE_NOW)
    add_run(sqlite_engine, tid, RunState.success, LAST)
    failed = add_run(sqlite_engine, tid, RunState.failed, at(2026, 6, 3, 9, 5), seconds=60)
    rec = Recorder()
    monkeypatch.setattr(kuma_sync, "push_heartbeat", rec)
    monkeypatch.setattr(notify, "engine", sqlite_engine)
    monkeypatch.setattr(due, "_now", lambda: STALE_NOW)
    await notify.dispatch(failed)
    assert [c[1] for c in rec.calls] == ["down"]


# --------------------------------------- notification outage and recovery


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _no_sleep(_):
    return None


@pytest.mark.asyncio
async def test_push_retries_a_bounded_number_of_times_then_reports_failure_without_the_token():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        raise httpx.ConnectError(f"cannot reach {request.url}")  # message holds the token URL

    async with make_client(handler) as c:
        result = await kuma_sync.push_heartbeat(TOKEN, "down", "m", client=c, sleep=_no_sleep)
    assert (result.ok, result.attempts) == (False, kuma_sync.PUSH_ATTEMPTS)
    assert len(seen) == kuma_sync.PUSH_ATTEMPTS
    assert result.error == "ConnectError" and TOKEN not in result.error


@pytest.mark.asyncio
async def test_push_5xx_is_retried_and_recovers():
    codes = [503, 502, 200]

    def handler(request):
        return httpx.Response(codes.pop(0))

    async with make_client(handler) as c:
        result = await kuma_sync.push_heartbeat(TOKEN, "up", client=c, sleep=_no_sleep)
    assert (result.ok, result.attempts) == (True, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [400, 401, 403, 404])
async def test_push_refusal_4xx_is_not_retried_and_is_reported_with_the_status(code):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(code)

    async with make_client(handler) as c:
        result = await kuma_sync.push_heartbeat(TOKEN, "down", client=c, sleep=_no_sleep)
    assert (result.ok, len(calls)) == (False, 1)
    assert result.error == f"HTTP {code}" and TOKEN not in result.error


@pytest.mark.asyncio
async def test_unconfigured_kuma_never_attempts_a_request():
    kuma_sync.settings.uptime_kuma_url = ""
    result = await kuma_sync.push_heartbeat(TOKEN, "up")
    assert (result.ok, result.attempts) == (False, 0)


@pytest.mark.asyncio
async def test_failed_delivery_stays_visible_then_recovers_on_a_later_sweep(sqlite_engine, tmp_path, clock):
    tid = add_task(sqlite_engine, tmp_path, STALE_NOW)
    add_run(sqlite_engine, tid, RunState.success, LAST)
    out = await kuma_sync.sweep(push=Recorder(ok=False), now=STALE_NOW)
    assert out[0].ok is False and out[0].pushed is True
    (failure,) = kuma_sync.failures()
    assert failure["task_id"] == tid and failure["kind"] == "push" and failure["error"] == "ConnectError"
    assert TOKEN not in repr(kuma_sync.failures())
    out = await kuma_sync.sweep(push=Recorder(ok=True), now=STALE_NOW)
    assert out[0].ok is True
    assert kuma_sync.failures() == []


@pytest.mark.asyncio
async def test_a_push_that_raises_is_a_failure_not_a_crash_or_a_success(sqlite_engine, tmp_path, clock):
    tid = add_task(sqlite_engine, tmp_path, STALE_NOW)
    add_run(sqlite_engine, tid, RunState.success, LAST)

    async def boom(token, status, msg=""):
        raise RuntimeError(f"secret url http://k/api/push/{token}")

    out = await kuma_sync.sweep(push=boom, now=STALE_NOW)
    assert out[0].ok is False and out[0].error == "RuntimeError"
    assert TOKEN not in repr(out) and TOKEN not in repr(kuma_sync.failures())


@pytest.mark.asyncio
async def test_one_task_failing_does_not_stop_the_others(sqlite_engine, tmp_path, clock):
    a = add_task(sqlite_engine, tmp_path, STALE_NOW)
    src = make_source(sqlite_engine, "other")
    b = make_task(sqlite_engine, tmp_path, src, 1, STALE_NOW, kuma_enabled=True, kuma_monitor_id=10, kuma_push_token="other-fixture")
    with Session(sqlite_engine) as s:
        t = s.get(Task, b)
        t.created_at = at(2026, 5, 1)
        s.add(t)
        s.commit()
    add_run(sqlite_engine, a, RunState.success, LAST)

    async def flaky(token, status, msg=""):
        if token == TOKEN:
            raise OSError("x")
        return kuma_sync.PushResult(True, 1)

    out = {o.task_id: o for o in await kuma_sync.sweep(push=flaky, now=STALE_NOW)}
    assert out[a].ok is False and out[b].ok is True


@pytest.mark.asyncio
async def test_monitor_configuration_failure_is_recorded_and_never_reported_as_sync(sqlite_engine, tmp_path, clock, monkeypatch):
    """_sync_kuma swallowed every failure into a log line; now it is on the operator API."""
    tid = add_task(sqlite_engine, tmp_path, STALE_NOW)

    class Refusing:
        async def update_interval(self, *_):
            raise RuntimeError("kuma editMonitor (interval) refused http://k/api/push/" + TOKEN)

        async def set_active(self, *_):
            raise AssertionError("must not run after a failed edit")

    monkeypatch.setattr(tasks_api.kuma, "client", lambda: Refusing())
    with Session(sqlite_engine) as s:
        t = s.get(Task, tid)
        await tasks_api._sync_kuma(t, s, prev_cron="0 4 * * *", prev_enabled=True)
    (f,) = kuma_sync.failures()
    assert (f["task_id"], f["kind"], f["error"]) == (tid, "configuration", "RuntimeError")
    assert TOKEN not in repr(f)
    # Recovery: a later successful sync clears it.

    class Working:
        async def update_interval(self, *_):
            return None

        async def set_active(self, *_):
            return None

    rec = Recorder()
    monkeypatch.setattr(tasks_api.kuma, "client", lambda: Working())
    monkeypatch.setattr(kuma_sync, "push_heartbeat", rec)
    with Session(sqlite_engine) as s:
        await tasks_api._sync_kuma(s.get(Task, tid), s, prev_cron="0 4 * * *", prev_enabled=True)
    assert kuma_sync.failures() == []


def test_the_scheduler_registers_the_freshness_sweep_as_its_own_job(sqlite_engine, monkeypatch):
    from pullbackup.services import scheduler

    monkeypatch.setattr(scheduler, "_scheduler", None)
    sched = scheduler.get_scheduler()
    monkeypatch.setattr(sched, "start", lambda *a, **k: None)
    scheduler.start()
    ids = {j.id for j in sched.get_jobs()}
    assert kuma_sync.SWEEP_JOB_ID in ids and scheduler.HEARTBEAT_JOB_ID in ids
    assert kuma_sync.SWEEP_JOB_ID != scheduler.HEARTBEAT_JOB_ID


# ------------------------------------------------- API / Kuma / system parity


@pytest.mark.asyncio
async def test_api_and_kuma_consume_exactly_the_same_projection(sqlite_engine, tmp_path, clock, monkeypatch):
    clock.now = STALE_NOW
    monkeypatch.setattr(due, "_now", lambda: STALE_NOW)
    tid = add_task(sqlite_engine, tmp_path, STALE_NOW)
    add_run(sqlite_engine, tid, RunState.success, LAST)
    add_run(sqlite_engine, tid, RunState.failed, at(2026, 6, 3, 9, 5), seconds=60)
    with Session(sqlite_engine) as s:
        s.add(TaskDue(task_id=tid, due_at=at(2026, 6, 2, 9), status=due.RETRY, attempts=1))
        s.commit()
        direct = freshness.project_task(s, s.get(Task, tid), now=STALE_NOW)
        out = tasks_api._to_out(s.get(Task, tid), s)
        listing = system_api.scheduler_health(s)
    rec = Recorder()
    (outcome,) = await kuma_sync.sweep(push=rec, now=STALE_NOW)
    assert out.freshness.model_dump() == direct.as_dict()
    assert out.last_success_at == direct.last_success_at
    assert (outcome.state, outcome.reason, outcome.status) == (direct.state, direct.reason, direct.kuma_status)
    assert rec.calls[0][1] == direct.kuma_status
    assert listing["stale_task_ids"] == [tid] and listing["stale_count"] == 1


@pytest.mark.asyncio
async def test_parity_holds_for_every_state_the_projection_can_emit(sqlite_engine, tmp_path, clock, monkeypatch):
    """One task per freshness state; API == direct projection == Kuma outcome."""
    scenarios = {
        freshness.FRESH: (at(2026, 6, 3, 20), True, [(RunState.success, at(2026, 6, 3, 9, 0, 30))], False),
        freshness.DELAYED: (at(2026, 6, 3, 9, 30), True, [(RunState.success, at(2026, 6, 2, 9, 0, 30))], False),
        freshness.STALE: (STALE_NOW, True, [(RunState.success, LAST)], False),
        freshness.NOT_YET_DUE: (at(2026, 6, 1, 13), True, [], True),
        freshness.DISABLED: (STALE_NOW, False, [(RunState.success, LAST)], False),
    }
    assert set(scenarios) == set(freshness.STATES)
    for state, (now, enabled, runs, fresh_epoch) in scenarios.items():
        monkeypatch.setattr(due, "_now", lambda now=now: now)
        tid = add_task(sqlite_engine, tmp_path, now, enabled=enabled, source=f"src-{state}")
        for st, started in runs:
            add_run(sqlite_engine, tid, st, started)
        if fresh_epoch:
            with Session(sqlite_engine) as s:
                t = s.get(Task, tid)
                t.created_at = at(2026, 6, 1, 12)
                s.add(t)
                s.commit()
        with Session(sqlite_engine) as s:
            direct = freshness.project_task(s, s.get(Task, tid), now=now)
            api = tasks_api._to_out(s.get(Task, tid), s).freshness
        assert direct.state == state, state
        assert api.model_dump() == direct.as_dict()
        outcome = await kuma_sync.sync_task(tid, push=Recorder(), now=now)
        assert (outcome.state, outcome.status) == (direct.state, direct.kuma_status)
        # (runs are left in place; each scenario owns its own task)


# ------------------------------------------------------- reconciliation tool


class FakeKuma:
    def __init__(self, monitors, fail_edit=0, fail_get=()):
        self.monitors = monitors
        self.calls = []
        self.fail_edit = fail_edit
        self.fail_get = set(fail_get)

    async def get_monitor(self, monitor_id):
        self.calls.append(("get", monitor_id))
        if monitor_id in self.fail_get:
            raise ConnectionError("down http://k/" + TOKEN)
        return dict(self.monitors[monitor_id])

    async def update_interval(self, monitor_id, interval):
        self.calls.append(("edit", monitor_id, interval))
        if self.fail_edit:
            self.fail_edit -= 1
            raise RuntimeError("refused http://k/" + TOKEN)
        self.monitors[monitor_id]["interval"] = interval


def seed_monitored(engine, tmp_path, n=2):
    ids = []
    for i in range(n):
        src = make_source(engine, f"s{i}")
        ids.append(make_task(engine, tmp_path, src, i, STALE_NOW, cron="0 3 * * *",
                             kuma_enabled=True, kuma_monitor_id=100 + i, kuma_push_token=f"fixture-{i}"))
    return ids


@pytest.mark.asyncio
async def test_dry_run_reports_and_never_mutates(sqlite_engine, tmp_path, clock):
    seed_monitored(sqlite_engine, tmp_path)
    fake = FakeKuma({100: {"interval": 86400, "active": True}, 101: {"interval": 86400, "active": False}})
    with Session(sqlite_engine) as s:
        report = await kuma_reconcile.plan(s, fake)
    assert not report.applied and report.ok
    assert [i.action for i in report.items] == ["update", "update"]
    assert all(c[0] == "get" for c in fake.calls)
    assert report.items[1].monitor_active is False
    assert TOKEN not in report.to_json() and "fixture-" not in report.to_json()


@pytest.mark.asyncio
async def test_apply_changes_only_the_interval_and_never_resumes_a_paused_monitor(sqlite_engine, tmp_path, clock):
    seed_monitored(sqlite_engine, tmp_path)
    want = freshness.kuma_expiry_seconds("0 3 * * *")
    fake = FakeKuma({100: {"interval": 86400, "active": True}, 101: {"interval": 86400, "active": False}})
    with Session(sqlite_engine) as s:
        report = await kuma_reconcile.apply(s, fake, sleep=_no_sleep)
    assert report.applied and report.ok
    assert fake.monitors[100]["interval"] == want and fake.monitors[101]["interval"] == want
    assert {c[0] for c in fake.calls} == {"get", "edit"}  # no pause/resume/delete/add exists on the API at all
    assert fake.monitors[101]["active"] is False
    # idempotent: a second run finds nothing to do
    with Session(sqlite_engine) as s:
        again = await kuma_reconcile.plan(s, fake)
    assert [i.action for i in again.items] == ["ok", "ok"]


@pytest.mark.asyncio
async def test_reconcile_failure_is_bounded_reported_and_not_success(sqlite_engine, tmp_path, clock):
    seed_monitored(sqlite_engine, tmp_path, n=1)
    fake = FakeKuma({100: {"interval": 86400}}, fail_edit=99)
    with Session(sqlite_engine) as s:
        report = await kuma_reconcile.apply(s, fake, sleep=_no_sleep)
    assert not report.ok
    (item,) = report.items
    assert (item.action, item.attempts, item.error) == ("failed", kuma_reconcile.RETRY_ATTEMPTS, "RuntimeError")
    assert sum(1 for c in fake.calls if c[0] == "edit") == kuma_reconcile.RETRY_ATTEMPTS
    assert "fixture-" not in report.to_json() and TOKEN not in report.to_json()


@pytest.mark.asyncio
async def test_reconcile_recovers_after_a_transient_refusal_and_get_failure_is_reported(sqlite_engine, tmp_path, clock):
    seed_monitored(sqlite_engine, tmp_path)
    fake = FakeKuma({100: {"interval": 86400}, 101: {"interval": 86400}}, fail_edit=1, fail_get={101})
    with Session(sqlite_engine) as s:
        report = await kuma_reconcile.apply(s, fake, sleep=_no_sleep)
    by = {i.monitor_id: i for i in report.items}
    assert by[100].action == "updated" and by[100].attempts == 2
    assert by[101].action == "failed" and by[101].error == "ConnectionError"
    assert not report.ok


@pytest.mark.asyncio
async def test_reconcile_ignores_unlinked_and_disabled_kuma_tasks(sqlite_engine, tmp_path, clock):
    src = make_source(sqlite_engine)
    make_task(sqlite_engine, tmp_path, src, 0, STALE_NOW, kuma_enabled=True, kuma_monitor_id=None)
    make_task(sqlite_engine, tmp_path, src, 1, STALE_NOW, kuma_enabled=False, kuma_monitor_id=5)
    fake = FakeKuma({})
    with Session(sqlite_engine) as s:
        report = await kuma_reconcile.plan(s, fake)
    assert report.items == [] and fake.calls == []


def test_reconcile_main_refuses_without_kuma_configuration(monkeypatch, capsys):
    monkeypatch.setattr(kuma_sync.settings, "uptime_kuma_url", "")
    assert kuma_reconcile.main([]) == 2
