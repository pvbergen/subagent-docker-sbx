#!/bin/sh
# Install the worker's dependencies into a venv inside the sandbox (outside the shared
# workspace, so nothing lands on the host). Runs as the sandbox's `agent` user.
set -eu

VENV=/home/agent/venv

if ! command -v uv >/dev/null 2>&1; then
  python3 -m pip install --user --quiet uv 2>/dev/null \
    || python3 -m pip install --user --quiet --break-system-packages uv
  PATH="$HOME/.local/bin:$PATH"
fi

uv venv --quiet --allow-existing --python ">=3.11" "$VENV"
uv pip install --quiet --python "$VENV/bin/python" -r requirements.txt
