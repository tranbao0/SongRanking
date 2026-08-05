#!/usr/bin/env bash
# Runs SQL (piped on stdin) against the hosted registry database via the
# turso CLI - the one stable entry point grouping_audit.py's headless
# agent is told to use, e.g.:
#
#   echo "UPDATE videos SET song_id = 42 WHERE video_id = 'abc123'" | bash scripts/turso_shell.sh
#
# Piping SQL in rather than passing it as a command-line argument avoids
# nested-quoting problems entirely (SQL string literals use single quotes,
# which would otherwise have to survive escaping through every shell layer
# between the caller and the turso CLI itself).
#
# The turso CLI ships no native Windows build (Linux/macOS only as of
# writing), so on Windows this has to relay through WSL - see README's
# "Hosted registry database (Turso)" section. Two gotchas found the hard
# way, both handled here: a bare `turso` isn't on WSL's non-interactive
# `bash -c` PATH (the installer only wires it into an *interactive*
# shell's .bashrc), so the full binary path is used instead; and Git
# Bash's MSYS runtime rewrites any argument that looks like a POSIX path
# before exec'ing `wsl`, silently mangling that same path unless
# MSYS_NO_PATHCONV disables it.
set -euo pipefail

DB_NAME="${TURSO_DB_NAME:-registry}"

if [[ "${OSTYPE:-}" == "msys" || "${OSTYPE:-}" == "cygwin" ]]; then
    MSYS_NO_PATHCONV=1 wsl -- bash -c "cat | ~/.turso/turso db shell '$DB_NAME'"
else
    turso db shell "$DB_NAME"
fi
