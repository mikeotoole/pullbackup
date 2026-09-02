import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".gitea" / "workflows" / "tests.yml"


def _steps(workflow: str) -> list[str]:
    """Split the workflow into per-step text blocks."""
    marker = "      - name: "
    parts = workflow.split(marker)
    return [marker + part for part in parts[1:]]


def _run_scripts(workflow: str) -> list[str]:
    """Return each `run: |` block body from the workflow."""
    blocks = []
    lines = workflow.splitlines()
    for index, line in enumerate(lines):
        if not line.strip().startswith("run: |"):
            continue
        indent = len(line) - len(line.lstrip())
        body = []
        for following in lines[index + 1 :]:
            if following.strip() and (len(following) - len(following.lstrip())) <= indent:
                break
            body.append(following)
        blocks.append("\n".join(body))
    return blocks


def test_factory_tests_workflow_is_exact_head_isolated_and_complete():
    workflow = WORKFLOW.read_text()

    required = [
        "pull_request_target:",
        "push:",
        "runs-on: review-isolated",
        "python:3.13.11-slim-bookworm@sha256:",
        "TARGET: ${{ github.event.pull_request.head.sha || github.sha }}",
        'test "$(git -C "$RUN_ROOT/work" rev-parse HEAD)" = "$TARGET"',
        "pytest -p no:cacheprovider -q",
        "npm ci --no-audit --no-fund --prefix frontend",
        "npm test --prefix frontend",
        "npm run build --prefix frontend",
    ]
    for marker in required:
        assert marker in workflow

    checkout_step, test_step = workflow.split(
        "      - name: Run canonical gates without credentials", maxsplit=1
    )
    assert "GITEA_TOKEN: ${{ secrets.REVIEWER_BOT_TOKEN }}" in checkout_step
    assert "GITEA_TOKEN" not in test_step


def test_workflow_steps_are_posix_shell_compatible():
    """Guard the dash incompatibility that silently disabled every gate.

    act_runner executes `run:` blocks with `sh -e {0}`, and /bin/sh in the
    pinned container image is dash. `set -o pipefail` is a bashism: it aborts
    the step with 'Illegal option -o pipefail' before any test can run, which
    reads as an infrastructure failure rather than a gate failure.
    """
    scripts = _run_scripts(WORKFLOW.read_text())
    assert scripts, "expected at least one multi-line run: block"

    for script in scripts:
        assert "pipefail" not in script, (
            "pipefail is unavailable in dash, the container's /bin/sh; "
            "use `set -eu` and avoid masking exit status behind a pipeline"
        )

    for script in scripts:
        for statement in re.findall(r"^\s*set\s+-\S+", script, flags=re.MULTILINE):
            assert set(statement.split("-", 1)[1]) <= set("eu"), (
                f"unsupported shell options in POSIX sh: {statement.strip()!r}"
            )


def test_multi_line_run_blocks_enable_strict_shell_mode():
    """Every multi-line step must still fail closed on the first error."""
    for script in _run_scripts(WORKFLOW.read_text()):
        assert re.search(r"^\s*set -eu\s*$", script, flags=re.MULTILINE), (
            "each multi-line run: block must start with `set -eu` so a failing "
            "command aborts the step instead of silently continuing"
        )


def test_npm_invocations_have_node_on_path():
    """Guard the exit-127 failure that skipped every gate after the pipefail fix.

    `npm` ships as a script with a `#!/usr/bin/env node` shebang, so invoking it
    without /opt/node/bin on PATH fails with
    "/usr/bin/env: 'node': No such file or directory" (exit 127) even though
    /opt/node/bin/node itself runs fine.
    """
    for script in _run_scripts(WORKFLOW.read_text()):
        lines = script.splitlines()
        npm_lines = [
            index
            for index, line in enumerate(lines)
            if re.search(r"(^|[\s/])npm\s", line)
        ]
        if not npm_lines:
            continue

        export_lines = [
            index
            for index, line in enumerate(lines)
            if "/opt/node/bin" in line and "PATH" in line
        ]
        assert export_lines, (
            "a step invoking npm must put /opt/node/bin on PATH; "
            "npm's env-based shebang fails with exit 127 otherwise"
        )
        assert min(export_lines) < min(npm_lines), (
            "PATH must be extended with /opt/node/bin before the first npm call"
        )


def test_backend_gate_creates_its_configured_data_directory():
    """Guard the 5 collection errors that made a green suite exit 1.

    The backend gate sets PULLBACKUP_DATA_DIR, but nothing creates it. SQLModel
    opens the SQLite file without creating its parent, so the fixtures in
    tests/test_bounded_auth_stores.py die with
    "sqlite3.OperationalError: unable to open database file". The run reads
    "341 passed, 3 skipped, 5 errors" and exits 1 — an infrastructure failure
    that looks exactly like a code verdict.

    The mkdir must reference the configured env var, not a second hard-coded
    literal: two independent copies of the path can drift apart silently, and
    the drift would only surface as this same opaque failure.
    """
    steps = [step for step in _steps(WORKFLOW.read_text()) if "pytest" in step]
    assert steps, "expected at least one step invoking pytest"

    for step in steps:
        name = step.splitlines()[0].split("- name: ", 1)[1].strip()
        env_block, run_block = step.split("        run:", 1)
        assert re.search(
            r"^\s*PULLBACKUP_DATA_DIR:\s*\S+\s*$", env_block, flags=re.MULTILINE
        ), f"step {name!r} runs pytest without declaring PULLBACKUP_DATA_DIR"

        lines = run_block.splitlines()
        mkdir_lines = [
            index
            for index, line in enumerate(lines)
            if re.search(r'^\s*mkdir -p "\$(\{)?PULLBACKUP_DATA_DIR(\})?"\s*$', line)
        ]
        pytest_lines = [
            index for index, line in enumerate(lines) if re.search(r"(^|\s)pytest\s", line)
        ]
        assert mkdir_lines, (
            f"step {name!r} must create the configured PULLBACKUP_DATA_DIR "
            'with `mkdir -p "$PULLBACKUP_DATA_DIR"`; SQLite will not create '
            "the parent directory of its database file"
        )
        assert min(mkdir_lines) < min(pytest_lines), (
            f"step {name!r} must create PULLBACKUP_DATA_DIR before the backend gate"
        )


def test_uv_uses_copy_link_mode():
    """Guard the uv clone failure on the runner's overlay filesystem.

    uv defaults to reflink/hardlink when populating build environments. On the
    runner that fails with "Resource temporarily unavailable (os error 11)"
    while installing build-system.requires, aborting the gates step before any
    test executes. UV_LINK_MODE=copy forces a portable plain-copy strategy.

    The setting is asserted per-step: a workflow-wide substring check would
    still pass if UV_LINK_MODE drifted to an unrelated step, leaving the uv
    invocation unprotected.

    Deliberately implemented with plain text parsing rather than PyYAML, which
    is not a declared test dependency: an optional import would let this guard
    silently skip in an environment where it is most needed.
    """
    uv_steps = [step for step in _steps(WORKFLOW.read_text()) if "uv run" in step]
    assert uv_steps, "expected at least one step invoking uv"

    for step in uv_steps:
        name = step.splitlines()[0].split("- name: ", 1)[1].strip()
        env_block = step.split("        run:", 1)[0]
        assert re.search(r"^\s*UV_LINK_MODE:\s*copy\s*$", env_block, flags=re.MULTILINE), (
            f"step {name!r} invokes uv but does not set UV_LINK_MODE=copy "
            "in its own env block"
        )
