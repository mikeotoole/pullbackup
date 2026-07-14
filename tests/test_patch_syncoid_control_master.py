import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PATCH_HELPER = REPOSITORY_ROOT / "scripts" / "patch_syncoid_control_master.py"
DOCKERFILE = REPOSITORY_ROOT / "Dockerfile"

OLD_MASTER_LAUNCH = """\
\t\topen FH, "$sshcmd -M -S $socket -o ControlPersist=1m $args{'sshport'} $rhost exit |";
\t\tclose FH;
"""
UNCHECKED_MASTER_LAUNCH = """\
\t\tsystem("$sshcmd -fN -M -S $socket -o ControlPersist=1m $args{'sshport'} $rhost");
"""
PATCHED_MASTER_LAUNCH = """\
\t\tsystem("$sshcmd -fN -M -S $socket -o ControlPersist=1m $args{'sshport'} $rhost") == 0
\t\t\tor do {
\t\t\t\twarn "FATAL: Unable to establish SSH control master to $rhost\\n";
\t\t\t\texit(2);
\t\t\t};
"""


class PatchSyncoidControlMasterTests(unittest.TestCase):
    def test_replaces_the_known_master_launch_block(self):
        original = (
            "#!/usr/bin/perl\n"
            "use strict;\n"
            "sub create_ssh_master {\n"
            f"{OLD_MASTER_LAUNCH}"
            "}\n"
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            syncoid = Path(temporary_directory) / "syncoid"
            syncoid.write_text(original)

            result = subprocess.run(
                [sys.executable, str(PATCH_HELPER), str(syncoid)],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                syncoid.read_text(),
                original.replace(OLD_MASTER_LAUNCH, PATCHED_MASTER_LAUNCH),
            )

    def test_master_launch_failure_is_fail_closed(self):
        original = "#!/usr/bin/perl\n" + OLD_MASTER_LAUNCH

        with tempfile.TemporaryDirectory() as temporary_directory:
            syncoid = Path(temporary_directory) / "syncoid"
            syncoid.write_text(original)

            result = subprocess.run(
                [sys.executable, str(PATCH_HELPER), str(syncoid)],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            patched = syncoid.read_text()
            self.assertEqual(
                patched,
                original.replace(OLD_MASTER_LAUNCH, PATCHED_MASTER_LAUNCH),
            )
            self.assertNotIn(UNCHECKED_MASTER_LAUNCH, patched)

    def test_preserves_target_file_mode(self):
        original = "#!/usr/bin/perl\n" + OLD_MASTER_LAUNCH

        with tempfile.TemporaryDirectory() as temporary_directory:
            syncoid = Path(temporary_directory) / "syncoid"
            syncoid.write_text(original)
            syncoid.chmod(0o751)

            result = subprocess.run(
                [sys.executable, str(PATCH_HELPER), str(syncoid)],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(syncoid.stat().st_mode), 0o751)

    def test_rejects_input_without_the_expected_block(self):
        original = "#!/usr/bin/perl\nuse strict;\n"

        with tempfile.TemporaryDirectory() as temporary_directory:
            syncoid = Path(temporary_directory) / "syncoid"
            syncoid.write_text(original)

            result = subprocess.run(
                [sys.executable, str(PATCH_HELPER), str(syncoid)],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("expected exactly one unpatched master-launch block", result.stderr)
            self.assertEqual(syncoid.read_text(), original)

    def test_rejects_duplicate_expected_blocks(self):
        original = OLD_MASTER_LAUNCH + "sub unrelated {}\n" + OLD_MASTER_LAUNCH

        with tempfile.TemporaryDirectory() as temporary_directory:
            syncoid = Path(temporary_directory) / "syncoid"
            syncoid.write_text(original)

            result = subprocess.run(
                [sys.executable, str(PATCH_HELPER), str(syncoid)],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("expected exactly one unpatched master-launch block; found 2", result.stderr)
            self.assertEqual(syncoid.read_text(), original)

    def test_rejects_already_patched_input(self):
        original = "#!/usr/bin/perl\n" + PATCHED_MASTER_LAUNCH

        with tempfile.TemporaryDirectory() as temporary_directory:
            syncoid = Path(temporary_directory) / "syncoid"
            syncoid.write_text(original)

            result = subprocess.run(
                [sys.executable, str(PATCH_HELPER), str(syncoid)],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already contains the patched master-launch block", result.stderr)
            self.assertEqual(syncoid.read_text(), original)

    def test_dockerfile_applies_and_validates_the_patch_during_sanoid_install(self):
        dockerfile = DOCKERFILE.read_text()
        copy_command = (
            "COPY scripts/patch_syncoid_control_master.py "
            "/tmp/patch_syncoid_control_master.py"
        )
        patch_command = (
            "python3 /tmp/patch_syncoid_control_master.py /usr/sbin/syncoid"
        )
        syntax_check = "perl -c /usr/sbin/syncoid"
        cleanup = "rm /tmp/patch_syncoid_control_master.py"

        self.assertIn(copy_command, dockerfile)
        self.assertLess(dockerfile.index("sanoid zfsutils-linux"), dockerfile.index(patch_command))
        self.assertLess(dockerfile.index(patch_command), dockerfile.index(syntax_check))
        self.assertLess(dockerfile.index(syntax_check), dockerfile.index(cleanup))


if __name__ == "__main__":
    unittest.main()
