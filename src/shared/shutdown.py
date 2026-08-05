"""
Cooperative Ctrl+C handling, install()ed once from run.py.

Killing python.exe from outside the process (Task Manager, a stray
`taskkill`) gives nothing a chance to finish cleanly - worst case is an
orphaned yt-dlp/ffmpeg subprocess, or db.pull_to_local()/push_from_local()
cut off mid-request in a way that leaves one chunk's local commit
desynced from what actually landed on the hosted registry (see
db.py's module docstring: push_from_local upserts before it ever
deletes, specifically so an interruption there can leave stale rows
behind but never lose real ones - this handler is a second, narrower
layer on top of that, since it can only ever catch Ctrl+C, never an
outside kill). Ctrl+C, handled here, is the safe way to stop instead:
the first press asks the current step to wrap up before exiting; a
second press forces an immediate exit if the first one doesn't return
promptly.
"""

import signal

_stop_requested = False


def _handle_sigint(signum, frame):
    global _stop_requested
    if _stop_requested:
        print("\n[!] Force quit - stopping immediately. Any yt-dlp/ffmpeg "
              "subprocess still running may be left orphaned; check with "
              "tasklist if that matters.")
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
    partway through - db.py's Turso pull/push in particular, each chunk
    request of which pairs a network round trip with its own commit (see
    db.py's _run_chunked/_push_chunk). A stop request made during the
    block is still honored - it just waits until the block's own
    __exit__ restores normal handling.
    """

    def __enter__(self):
        self._previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        return self

    def __exit__(self, exc_type, exc, tb):
        signal.signal(signal.SIGINT, self._previous)
        if _stop_requested:
            print("[!] Stopping now that this network step has finished safely.")
        return False
