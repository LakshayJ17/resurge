"""OS integration: keep the machine awake while an engagement runs.

On macOS a long run (especially with local models) can stall for hours when the Mac idles to sleep; we observed
this in testing. ``keep_awake()`` holds an *idle*-sleep assertion via the built-in ``caffeinate`` tool:

* tied to this process (``caffeinate -i -w <pid>``) for CLI runs - released automatically when the process exits;
* or untied (``caffeinate -i``) and released by ``release()`` for runs inside a long-lived server (web UI, MCP).

Lid-close still sleeps a MacBook (macOS policy); other platforms are a no-op.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def keep_awake(tie_to_process: bool = True) -> subprocess.Popen | None:
    if sys.platform != "darwin" or os.getenv("RESURGE_KEEP_AWAKE", "1") == "0" or not shutil.which("caffeinate"):
        return None
    cmd = ["caffeinate", "-i"] + (["-w", str(os.getpid())] if tie_to_process else [])
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def release(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
