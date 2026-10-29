"""What a sandboxed worker needs inside its VM.

This is the single source for worker dependencies. A worker sandbox runs ``langgraph dev`` for
the worker's graph, so its image or template must provide:

- Python >= 3.11 and ``uv`` (the default ``shell`` template ships both).
- A venv at ``VENV_DIR`` with ``WORKER_REQUIREMENTS`` installed.
- The worker's own dependencies from ``requirements.txt`` in its directory, typically the model
  provider integration (for example ``langchain-openai``) and any tool libraries.
- The sbx base-image floor: an ``agent`` user with UID 1000 owning ``/home/agent``,
  ``/bin/sh`` and ``/bin/bash``, curl, git and CA certificates.

Without a template, `SandboxedWorker` installs the venv on every start with `INSTALL_SCRIPT`. A
template (``WorkerSpec.template``) that already contains the venv skips that step.
``docker/worker.Dockerfile`` builds one from any Debian- or Ubuntu-based image; keep it in sync
with this module (a unit test checks the requirements and venv path).
"""

VENV_DIR = "/home/agent/venv"

WORKER_REQUIREMENTS: tuple[str, ...] = (
    "deepagents>=0.6.7",
    # `langgraph dev` with the in-memory runtime.
    "langgraph-cli[inmem]>=0.4.38",
)

# Runs in the worker directory as the sandbox's `agent` user. $1 is the venv path, the remaining
# arguments are requirement specifiers. Installs into the VM, outside the shared workspace, so
# nothing lands on the host.
INSTALL_SCRIPT = """
set -eu
venv="$1"
shift
if ! command -v uv >/dev/null 2>&1; then
  python3 -m pip install --user --quiet uv 2>/dev/null \\
    || python3 -m pip install --user --quiet --break-system-packages uv
  PATH="$HOME/.local/bin:$PATH"
fi
uv venv --quiet --allow-existing --python ">=3.11" "$venv"
uv pip install --quiet --python "$venv/bin/python" "$@"
if [ -f requirements.txt ]; then
  uv pip install --quiet --python "$venv/bin/python" -r requirements.txt
fi
"""
