"""Serve a LangGraph/Deep Agents graph from inside its own sbx sandbox.

Prototype for running each agent of a workflow in its own microVM: the sandbox gets the
worker directory as its workspace, installs the worker's dependencies into a venv inside the
VM, and runs `langgraph dev` there. The host only talks to it over a port published on
127.0.0.1, for example through deepagents' `AsyncSubAgent(url=...)`.
"""

from __future__ import annotations

import atexit
import logging
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import weakref
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path

from deepagents_docker_sbx._sbx import (
    SBX_BINARY,
    format_sbx_error,
    run_sbx,
    sbx_unavailable_reason,
)
from deepagents_docker_sbx.errors import SBXError

logger = logging.getLogger(__name__)

SANDBOX_AGENT = "shell"
SANDBOX_PORT = 2024
VENV_DIR = "/home/agent/venv"
# Placeholder the sbx proxy swaps for the key stored with `sbx secret set openai`.
PROXY_MANAGED_KEY = "proxy-managed"

SBX_CHECK_TIMEOUT = 30
CREATE_TIMEOUT = 600
INSTALL_TIMEOUT = 900
READY_TIMEOUT = 180
CLEANUP_TIMEOUT = 60
_READY_POLL_INTERVAL = 1.0
_LOG_TAIL_BYTES = 4000


def kit_args(kits: Sequence[str | Path]) -> list[str]:
    """One `--kit` per kit. Local kit directories are passed as absolute paths."""
    args: list[str] = []
    for kit in kits:
        args += ["--kit", str(kit.resolve()) if isinstance(kit, Path) else kit]
    return args


def build_create_args(
    name: str,
    worker_dir: Path,
    host_port: int,
    *,
    agent: str = SANDBOX_AGENT,
    kits: Sequence[str | Path] = (),
    template: str | None = None,
) -> list[str]:
    """`sbx create` args: worker dir as workspace, port on host loopback only.

    `agent` is a built-in agent name or a v3 workload reference; `template` is a local image
    saved with `sbx template save`.
    """
    args = [
        "create",
        "--name",
        name,
        "--no-progress",
        "--publish",
        f"127.0.0.1:{host_port}:{SANDBOX_PORT}",
        *kit_args(kits),
    ]
    if template is not None:
        args += ["--template", template]
    return [*args, agent, str(worker_dir)]


def build_template_save_args(name: str, template: str) -> list[str]:
    """`sbx template save` args. The template stays in the local image store."""
    return ["template", "save", name, template]


def build_install_args(name: str, worker_dir: Path) -> list[str]:
    """`sbx exec` args that run the worker's bootstrap script inside the sandbox."""
    return ["exec", "--workdir", str(worker_dir), name, "sh", "./bootstrap.sh"]


def build_serve_args(name: str, worker_dir: Path) -> list[str]:
    """`sbx exec` args that run `langgraph dev` in the sandbox, reachable on all interfaces."""
    return [
        "exec",
        "--env",
        f"OPENAI_API_KEY={PROXY_MANAGED_KEY}",
        "--workdir",
        str(worker_dir),
        name,
        f"{VENV_DIR}/bin/langgraph",
        "dev",
        "--host",
        "0.0.0.0",  # noqa: S104 - sandbox interface; the host side is bound to 127.0.0.1
        "--port",
        str(SANDBOX_PORT),
        "--no-browser",
        "--no-reload",
        # The in-memory dev server rejects sync I/O inside async code; deepagents tools do
        # some, which is harmless here.
        "--allow-blocking",
    ]


def _atexit_close(ref: weakref.ReferenceType[SandboxedAgentServer]) -> None:
    server = ref()
    if server is not None:
        server.close()


class SandboxedAgentServer:
    """Run `langgraph dev` for `worker_dir` inside a dedicated sbx sandbox."""

    def __init__(
        self,
        worker_dir: str | Path,
        *,
        host_port: int = SANDBOX_PORT,
        ready_timeout: float = READY_TIMEOUT,
        agent: str = SANDBOX_AGENT,
        kits: Sequence[str | Path] = (),
        template: str | None = None,
    ) -> None:
        self._worker_dir = Path(worker_dir).resolve()
        if not (self._worker_dir / "langgraph.json").is_file():
            msg = f"{self._worker_dir} has no langgraph.json"
            raise ValueError(msg)
        self._host_port = host_port
        self._ready_timeout = ready_timeout
        self._agent = agent
        self._kits = tuple(kits)
        # A saved template already holds the venv, so bootstrap.sh is skipped.
        self._template = template
        self._name = f"deepagents-agent-{uuid.uuid4().hex[:12]}"
        self._log_path = Path(tempfile.mkstemp(prefix=f"{self._name}-", suffix=".log")[1])
        self._process: subprocess.Popen[bytes] | None = None
        self._closed = False
        self._atexit_hook: Callable[[], None] | None = None

        self._atexit_hook = partial(_atexit_close, weakref.ref(self))
        atexit.register(self._atexit_hook)
        try:
            self._create()
            if self._template is None:
                self._install()
            self._serve()
            self._wait_until_ready()
        except BaseException:
            self.close()
            raise

    @property
    def name(self) -> str:
        """Sandbox name as shown by `sbx ls`."""
        return self._name

    @property
    def url(self) -> str:
        """Agent Protocol endpoint of the sandboxed server, as seen from the host."""
        return f"http://127.0.0.1:{self._host_port}"

    @property
    def log_path(self) -> Path:
        """Host file collecting the server's stdout/stderr."""
        return self._log_path

    def _run(self, args: list[str], *, timeout: float, what: str) -> None:
        result = run_sbx(args, timeout=timeout, max_output_bytes=_LOG_TAIL_BYTES * 4)
        if result.returncode != 0:
            msg = f"{what} failed: {format_sbx_error(result)}"
            raise SBXError(msg)

    def _create(self) -> None:
        reason = sbx_unavailable_reason(timeout=SBX_CHECK_TIMEOUT)
        if reason is not None:
            msg = f"sbx is not available ({reason})"
            raise SBXError(msg)
        logger.info("creating sandbox %s for %s", self._name, self._worker_dir)
        args = build_create_args(
            self._name,
            self._worker_dir,
            self._host_port,
            agent=self._agent,
            kits=self._kits,
            template=self._template,
        )
        self._run(args, timeout=CREATE_TIMEOUT, what="sbx create")

    def _install(self) -> None:
        logger.info("installing worker dependencies in %s", self._name)
        args = build_install_args(self._name, self._worker_dir)
        self._run(args, timeout=INSTALL_TIMEOUT, what="dependency install")

    def _serve(self) -> None:
        # `sbx exec` has no detached mode, so the host keeps the exec client running for the
        # server's lifetime.
        logger.info("starting langgraph dev in %s (log: %s)", self._name, self._log_path)
        with self._log_path.open("wb") as log:
            self._process = subprocess.Popen(  # noqa: S603
                [SBX_BINARY, *build_serve_args(self._name, self._worker_dir)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                msg = f"langgraph dev exited early:\n{self.log_tail()}"
                raise SBXError(msg)
            try:
                with urllib.request.urlopen(f"{self.url}/ok", timeout=5) as response:  # noqa: S310
                    if response.status == 200:
                        logger.info("sandboxed agent ready at %s", self.url)
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(_READY_POLL_INTERVAL)
        msg = f"server not ready after {self._ready_timeout}s:\n{self.log_tail()}"
        raise SBXError(msg)

    def log_tail(self) -> str:
        """Last few KB of the server log."""
        try:
            data = self._log_path.read_bytes()
        except OSError:
            return ""
        return data[-_LOG_TAIL_BYTES:].decode("utf-8", errors="replace")

    def save_template(self, template: str) -> None:
        """Stop the server and the sandbox, then save the sandbox as a local template.

        The template holds the VM filesystem, including the venv from `bootstrap.sh`, but not
        the mounted workspace. The sandbox stays stopped until `close()` removes it.
        """
        self._stop_server()
        self._run(["stop", self._name], timeout=CLEANUP_TIMEOUT, what="sbx stop")
        args = build_template_save_args(self._name, template)
        self._run(args, timeout=CREATE_TIMEOUT, what="sbx template save")

    def _stop_server(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()

    def close(self) -> None:
        """Stop the server and remove the sandbox."""
        if self._closed:
            return
        self._closed = True
        if self._atexit_hook is not None:
            atexit.unregister(self._atexit_hook)
        self._stop_server()
        try:
            run_sbx(["rm", "--force", self._name], timeout=CLEANUP_TIMEOUT)
        except SBXError:
            pass

    def __enter__(self) -> SandboxedAgentServer:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
