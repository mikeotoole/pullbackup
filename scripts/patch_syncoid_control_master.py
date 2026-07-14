#!/usr/bin/env python3
"""Patch Syncoid's SSH control-master launch during image builds."""

import sys
from pathlib import Path


OLD_MASTER_LAUNCH = b"""\
\t\topen FH, "$sshcmd -M -S $socket -o ControlPersist=1m $args{'sshport'} $rhost exit |";
\t\tclose FH;
"""
PATCHED_MASTER_LAUNCH = b"""\
\t\tsystem("$sshcmd -fN -M -S $socket -o ControlPersist=1m $args{'sshport'} $rhost") == 0
\t\t\tor do {
\t\t\t\twarn "FATAL: Unable to establish SSH control master to $rhost\\n";
\t\t\t\texit(2);
\t\t\t};
"""


def main() -> None:
    syncoid_path = Path(sys.argv[1])
    original = syncoid_path.read_bytes()
    occurrences = original.count(OLD_MASTER_LAUNCH)
    if PATCHED_MASTER_LAUNCH in original:
        raise SystemExit(
            "error: input already contains the patched master-launch block"
        )
    if occurrences == 0:
        raise SystemExit(
            "error: expected exactly one unpatched master-launch block; found 0"
        )
    if occurrences > 1:
        raise SystemExit(
            "error: expected exactly one unpatched master-launch block; "
            f"found {occurrences}"
        )
    syncoid_path.write_bytes(
        original.replace(OLD_MASTER_LAUNCH, PATCHED_MASTER_LAUNCH, 1)
    )


if __name__ == "__main__":
    main()
