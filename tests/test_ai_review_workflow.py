import re
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".gitea" / "workflows" / "ai-review.yml"


class AiReviewWorkflowTests(unittest.TestCase):
    def test_review_diff_includes_npm_lockfiles_but_excludes_generic_locks(self):
        workflow = WORKFLOW.read_text()
        pathspecs = re.findall(
            r"'(:\(exclude\)[^']+)'",
            workflow.split('> "$RUN_ROOT/pr.diff"', 1)[0].split(
                'git -C "$RUN_ROOT/work" diff', 1
            )[1],
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            subprocess.run(["git", "init", "-q", repository], check=True)
            subprocess.run(
                ["git", "-C", repository, "config", "user.name", "Test"], check=True
            )
            subprocess.run(
                ["git", "-C", repository, "config", "user.email", "test@example.com"],
                check=True,
            )
            (repository / "frontend").mkdir()
            (repository / "vendor").mkdir()
            (repository / "frontend" / "package-lock.json").write_text("old npm lock\n")
            (repository / "vendor" / "Gemfile.lock").write_text("old generic lock\n")
            subprocess.run(["git", "-C", repository, "add", "."], check=True)
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "base"], check=True
            )
            base = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"], text=True
            ).strip()

            (repository / "frontend" / "package-lock.json").write_text("new npm lock\n")
            (repository / "vendor" / "Gemfile.lock").write_text("new generic lock\n")
            subprocess.run(["git", "-C", repository, "add", "."], check=True)
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "head"], check=True
            )
            head = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"], text=True
            ).strip()

            changed_paths = subprocess.check_output(
                [
                    "git",
                    "-C",
                    repository,
                    "diff",
                    "--name-only",
                    base,
                    head,
                    "--",
                    ".",
                    *pathspecs,
                ],
                text=True,
            ).splitlines()

        self.assertIn("frontend/package-lock.json", changed_paths)
        self.assertNotIn("vendor/Gemfile.lock", changed_paths)


if __name__ == "__main__":
    unittest.main()
