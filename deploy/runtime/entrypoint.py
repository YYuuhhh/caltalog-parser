"""Validate the sealed runtime before starting an application command."""

import os
import subprocess
import sys

from verify_runtime import verify

try:
    verify("tools")
except (OSError, ValueError, subprocess.CalledProcessError) as exc:
    raise SystemExit(f"Dependency verification failed: {exc}")
if len(sys.argv) < 2:
    raise SystemExit("An application command is required")
os.execvp(sys.argv[1], sys.argv[1:])
