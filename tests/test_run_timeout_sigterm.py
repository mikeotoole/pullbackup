# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Mike O'Toole
"""The acceptance criterion, run against a subprocess that IGNORES SIGTERM.

The card asks for exactly this: "a deliberately hung subprocess (e.g. one that
ignores SIGTERM on a dead network path) results in a failed run within the
timeout window, and the next scheduled run of ANY task still fires."

Kept separate from tests/test_run_timeout.py because it is slow by
construction: the kill path escalates TERM -> 5s -> KILL, so a stub that traps
TERM cannot be reclaimed in less than that five seconds. The rest of the suite
uses cooperative stubs and stays fast.

This is the harder half of the guarantee. A timeout implemented as a plain
`proc.terminate()` passes every cooperative test in the suite and then fails
here — which is precisely the production case, since a process blocked in an
uninterruptible network wait is not going to politely honour a TERM either.
"""
import asyncio

import pytest
from pullbackup.models import Run, RunState
from pullbackup.services import runner
from sqlmodel import Session

from test_run_admission import (  # noqa: F401
    create_source_tasks,
    sqlite_engine,
)
from test_run_cancellation import (  # noqa: F401
    drain_leftover_executions,
    process_state,
    start_blocking_run,
    wait_until_dead,
)

# One second of run, then TERM, then the 5s escalation, then KILL and the
# terminal commit. Fifteen leaves generous headroom on a loaded CI runner
# without turning a hang into a silent 400s pytest timeout.
SETTLE_TIMEOUT_SECONDS = 15


@pytest.fixture
def sigterm_ignoring_rsync(tmp_path, monkeypatch):
    """An rsync that traps SIGTERM and keeps running, plus its own bound.

    `trap '' TERM` makes the shell ignore the signal entirely, so only SIGKILL
    ends it — the same effective behaviour as a process wedged in an
    uninterruptible wait on a dead socket.

    The 120-iteration self-bound is a safety net, not the mechanism: if the
    production code fails to escalate, this stub still exits eventually rather
    than leaving a spinning process behind on the CI runner. It is far longer
    than the test's own timeout, so it can never be what makes the test pass.
    """
    bin_dir = tmp_path / "stubborn"
    bin_dir.mkdir()
    stub = bin_dir / "rsync"
    stub.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "i=0\n"
        "while [ $i -lt 120 ]; do\n"
        "  sleep 1\n"
        "  i=$((i+1))\n"
        "done\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/bin:/usr/bin")
    return stub


@pytest.mark.asyncio
async def test_a_sigterm_ignoring_subprocess_is_killed_and_the_next_run_fires(
    sqlite_engine, monkeypatch, sigterm_ignoring_rsync
):
    monkeypatch.setattr(runner.settings, "run_timeout_seconds", 1)
    (task,) = create_source_tasks(sqlite_engine, count=1)
    run_id, execution, captured = await start_blocking_run(
        sqlite_engine, task.id, monkeypatch
    )
    process = captured["process"]

    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SETTLE_TIMEOUT_SECONDS
        state = None
        while loop.time() < deadline:
            with Session(sqlite_engine) as session:
                state = session.get(Run, run_id).state
            if state not in (RunState.pending, RunState.running):
                break
            await asyncio.sleep(0.1)

        assert state == RunState.failed, (
            f"a subprocess that ignores SIGTERM must still be killed and its "
            f"run failed within the timeout window; the row is {state}"
        )
        with Session(sqlite_engine) as session:
            run = session.get(Run, run_id)
        assert "timeout" in run.error_message.lower()

        # The stub only dies to SIGKILL, so its death IS the proof that the
        # escalation ran rather than stopping at TERM.
        assert await wait_until_dead(process.pid), (
            "the subprocess survived the timeout; TERM alone is not enough "
            "for a process that ignores it, the KILL escalation must run"
        )

        # The acceptance criterion proper: the schedule is unblocked.
        admitted = await runner.admit_run(task.id)
        assert admitted.accepted, (
            "after a stubborn hung run was killed the task must be "
            "schedulable again"
        )
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        if process.returncode is None:
            process.kill()
            await process.wait()
