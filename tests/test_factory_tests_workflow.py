from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".gitea" / "workflows" / "tests.yml"


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
