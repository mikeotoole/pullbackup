import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".gitea" / "workflows" / "tests.yml"


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
