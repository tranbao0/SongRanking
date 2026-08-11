"""
Cooperative Ctrl+C handling, install()ed once from run.py.

Killing python.exe from outside the process (Task Manager, a stray
`taskkill`) gives nothing a chance to finish cleanly - worst case is an
orphaned yt-dlp/ffmpeg subprocess, or db.push_from_local() cut off between
its git commit and push, leaving a locally-committed change not yet on the
data repo (see db.py's module docstring - harmless on its own, since a git
commit is transactional and the next push just carries it along, but this
handler is a second, narrower layer on top of that so it doesn't even get
a chance to happen from a Ctrl+C specifically, only from an outside kill
this handler can't catch at all). Ctrl+C, handled here, is the safe way to
stop instead: the first press asks the current step to wrap up before
exiting; a second press force-kills helper subprocesses (ffmpeg, yt-dlp
children) and exits immediately.
"""

import os
import signal
import subprocess
import sys

_stop_requested = False


def _child_pids(parent_pid: int) -> list[int]:
    """PIDs whose parent is `parent_pid`. Best-effort; empty on failure."""
    if sys.platform == "win32":
        # PowerShell is always present on the supported Windows hosts; wmic
        # is deprecated / sometimes missing on recent builds.
        try:
            result = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    f"(Get-CimInstance Win32_Process -Filter \"ParentProcessId={parent_pid}\").ProcessId",
                ],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return []
        pids = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids

    try:
        result = subprocess.run(
            ["ps", "-o", "pid=", "--ppid", str(parent_pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    pids = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def kill_helper_subprocesses() -> int:
    """
    Force-kill ffmpeg / yt-dlp (and any other) child processes of this
    interpreter, including their own children.

    Used on a second Ctrl+C so a hung encode/download doesn't survive the
    parent as an orphan. Returns how many direct child PIDs we tried to
    kill (0 if none / enumeration failed). Does not kill this process.
    """
    me = os.getpid()
    children = _child_pids(me)
    killed = 0

    if sys.platform == "win32":
        for pid in children:
            if pid == me:
                continue
            try:
                # /T = kill this PID and every descendant (ffmpeg nested
                # under a `python -m yt_dlp` child, etc.).
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, text=True, timeout=10,
                )
                killed += 1
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass
        # Belt-and-suspenders: a force-quit mid-encode can leave ffmpeg
        # reparented under pid 0 / services if the parent thread died
        # oddly. Harmless if none are running.
        try:
            subprocess.run(
                ["taskkill", "/IM", "ffmpeg.exe", "/F"],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        return killed

    for pid in children:
        if pid == me:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            pass
    return killed


def _handle_sigint(signum, frame):
    global _stop_requested
    if _stop_requested:
        n = kill_helper_subprocesses()
        if n:
            print(f"\n[!] Force quit - killed {n} helper subprocess tree(s) "
                  f"(ffmpeg / yt-dlp). Stopping immediately.")
        else:
            print("\n[!] Force quit - stopping immediately "
                  "(no helper subprocess trees found to kill).")
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        raise KeyboardInterrupt
    _stop_requested = True
    print("\n[!] Stop requested - finishing the current step, then exiting. "
          "Press Ctrl+C again to force quit immediately.")


def install() -> None:
    """Call once, from run.py's entry point."""
    signal.signal(signal.SIGINT, _handle_sigint)


def requested() -> bool:
    return _stop_requested


class critical_section:
    """
    Ignore Ctrl+C for the duration of a block that must not be cut off
    partway through - db.py's push_from_local() in particular, around its
    git commit + push (see db.py's module docstring). A stop request made
    during the block is still honored - it just waits until the block's
    own __exit__ restores normal handling.
    """

    def __enter__(self):
        self._previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        return self

    def __exit__(self, exc_type, exc, tb):
        signal.signal(signal.SIGINT, self._previous)
        if _stop_requested:
            print("[!] Stopping now that this network step has finished safely.")
        return False
