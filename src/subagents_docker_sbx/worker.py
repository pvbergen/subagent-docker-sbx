"""Run a Deep Agents worker inside its own sbx sandbox and expose it as an async subagent.

The sandbox gets the worker directory as its workspace, its own secrets and network rules, a
venv with the worker's dependencies, and runs ``langgraph dev`` there. The host only talks to it
through a port published on 127.0.0.1, for example from a supervisor's ``AsyncSubAgent``.
"""

from __future__ import annotations

import atexit
import logging
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import weakref
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Literal

from deepagents import AsyncSubAgent

from ._sbx import (
    SBX_BINARY,
    format_sbx_error,
    run_sbx,
    sbx_unavailable_reason,
)
from .errors import SBXError
from .requirements import INSTALL_SCRIPT, VENV_DIR, WORKER_REQUIREMENTS
from .spec import WorkerSpec

logger = logging.getLogger(__name__)

SANDBOX_PORT = 2024
# Placeholder the sbx proxy swaps for the secret stored with `sbx secret set`.
PROXY_MANAGED_VALUE = "proxy-managed"

SBX_CHECK_TIMEOUT = 30
CREATE_TIMEOUT = 600
INSTALL_TIMEOUT = 900
READY_TIMEOUT = 180
CLEANUP_TIMEOUT = 60
_READY_POLL_INTERVAL = 1.0
_LOG_TAIL_BYTES = 4000


def free_port() -> int:
    """A port on 127.0.0.1 that is free right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_create_args(spec: WorkerSpec, name: str, host_port: int) -> list[str]:
    """`sbx create` args: worker dir as workspace, port on host loopback only."""
    args = [
        "create",
        "--name",
        name,
        "--no-progress",
        "--publish",
        f"127.0.0.1:{host_port}:{SANDBOX_PORT}",
    ]
    if spec.template is not None:
        args += ["--template", spec.template]
    if spec.memory is not None:
        args += ["--memory", spec.memory]
    if spec.cpus is not None:
        args += ["--cpus", str(spec.cpus)]
    for kit in spec.kits:
        args += ["--kit", str(kit.resolve()) if isinstance(kit, Path) else kit]
    return [*args, spec.agent, str(spec.path)]


def build_secret_args(service: str, name: str, value: str) -> list[str]:
    """`sbx secret set` args for a secret scoped to one sandbox. Never log these."""
    return ["secret", "set", service, "--sandbox", name, "-t", value]


def build_policy_args(action: Literal["allow", "deny"], name: str, resource: str) -> list[str]:
    """`sbx policy allow|deny network` args for a rule scoped to one sandbox."""
    return ["policy", action, "network", "--sandbox", name, resource]


def build_install_args(spec: WorkerSpec, name: str) -> list[str]:
    """`sbx exec` args that install the worker venv inside the sandbox."""
    return [
        "exec",
        "--workdir",
        str(spec.path),
        name,
        "sh",
        "-c",
        INSTALL_SCRIPT,
        "sh",
        VENV_DIR,
        *WORKER_REQUIREMENTS,
    ]


def server_env(spec: WorkerSpec) -> dict[str, str]:
    """Environment for the worker server: placeholders for secrets plus `spec.env`."""
    env = {variable: PROXY_MANAGED_VALUE for variable in spec.secrets.values()}
    env.update(spec.env)
    return env


def build_serve_args(spec: WorkerSpec, name: str) -> list[str]:
    """`sbx exec` args that run `langgraph dev` in the sandbox, reachable on all interfaces."""
    env_args: list[str] = []
    for key, value in server_env(spec).items():
        env_args += ["--env", f"{key}={value}"]
    return [
        "exec",
        *env_args,
        "--workdir",
        str(spec.path),
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


def _secret_values(secrets: Mapping[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    missing: list[str] = []
    for service, variable in secrets.items():
        value = os.environ.get(variable)
        if value:
            values[service] = value
        else:
            missing.append(variable)
    if missing:
        msg = f"secret environment variables not set on the host: {', '.join(missing)}"
        raise ValueError(msg)
    return values


def _atexit_close(ref: weakref.ReferenceType[SandboxedWorker]) -> None:
    worker = ref()
    if worker is not None:
        worker.close()


class SandboxedWorker:
    """A worker agent served by `langgraph dev` from its own sbx sandbox."""

    def __init__(self, spec: WorkerSpec, *, ready_timeout: float = READY_TIMEOUT) -> None:
        self._spec = spec
        # Read before anything is created, so a missing variable leaves nothing behind.
        secret_values = _secret_values(spec.secrets)
        self._host_port = spec.host_port if spec.host_port is not None else free_port()
        self._ready_timeout = ready_timeout
        self._name = f"subagent-{_slug(spec.name)}-{uuid.uuid4().hex[:12]}"
        self._log_path = Path(tempfile.mkstemp(prefix=f"{self._name}-", suffix=".log")[1])
        self._process: subprocess.Popen[bytes] | None = None
        self._closed = False

        self._atexit_hook: Callable[[], None] = partial(_atexit_close, weakref.ref(self))
        atexit.register(self._atexit_hook)
        try:
            self._create()
            self._set_secrets(secret_values)
            self._apply_network_rules()
            if spec.template is None:
                self._install()
            self._serve()
            self._wait_until_ready()
        except BaseException:
            self.close()
            raise

    @property
    def spec(self) -> WorkerSpec:
        return self._spec

    @property
    def name(self) -> str:
        """Sandbox name as shown by `sbx ls`."""
        return self._name

    @property
    def url(self) -> str:
        """Agent Protocol endpoint of the worker, as seen from the host."""
        return f"http://127.0.0.1:{self._host_port}"

    @property
    def log_path(self) -> Path:
        """Host file collecting the server's stdout/stderr."""
        return self._log_path

    def subagent(self) -> AsyncSubAgent:
        """The worker as an async subagent for `create_deep_agent(subagents=[...])`."""
        return AsyncSubAgent(
            name=self._spec.name,
            description=self._spec.description,
            graph_id=self._spec.graph_id,
            url=self.url,
        )

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
        logger.info("creating sandbox %s for worker %s", self._name, self._spec.name)
        args = build_create_args(self._spec, self._name, self._host_port)
        self._run(args, timeout=CREATE_TIMEOUT, what="sbx create")

    def _set_secrets(self, values: Mapping[str, str]) -> None:
        for service, value in values.items():
            # The error text is safe to show; the arguments, which hold the value, are not.
            args = build_secret_args(service, self._name, value)
            self._run(args, timeout=SBX_CHECK_TIMEOUT, what=f"sbx secret set {service}")

    def _apply_network_rules(self) -> None:
        rules = [("allow", host) for host in self._spec.allow_network]
        rules += [("deny", host) for host in self._spec.deny_network]
        for action, host in rules:
            args = build_policy_args(action, self._name, host)
            self._run(args, timeout=SBX_CHECK_TIMEOUT, what=f"sbx policy {action} {host}")

    def _install(self) -> None:
        logger.info("installing worker dependencies in %s", self._name)
        args = build_install_args(self._spec, self._name)
        self._run(args, timeout=INSTALL_TIMEOUT, what="dependency install")

    def _serve(self) -> None:
        # `sbx exec` has no detached mode, so the host keeps the exec client running for the
        # server's lifetime.
        logger.info("starting langgraph dev in %s (log: %s)", self._name, self._log_path)
        with self._log_path.open("wb") as log:
            self._process = subprocess.Popen(  # noqa: S603
                [SBX_BINARY, *build_serve_args(self._spec, self._name)],
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
                        logger.info("worker %s ready at %s", self._spec.name, self.url)
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(_READY_POLL_INTERVAL)
        msg = f"worker not ready after {self._ready_timeout}s:\n{self.log_tail()}"
        raise SBXError(msg)

    def log_tail(self) -> str:
        """Last few KB of the server log."""
        try:
            data = self._log_path.read_bytes()
        except OSError:
            return ""
        return data[-_LOG_TAIL_BYTES:].decode("utf-8", errors="replace")

    def close(self) -> None:
        """Stop the server and remove the sandbox, with its secrets and network rules."""
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self._atexit_hook)
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        try:
            run_sbx(["rm", "--force", self._name], timeout=CLEANUP_TIMEOUT)
        except SBXError:
            pass

    def __enter__(self) -> SandboxedWorker:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _slug(name: str) -> str:
    """Lowercase alphanumerics and dashes, for use in a sandbox name."""
    slug = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    return slug[:24] or "worker"
