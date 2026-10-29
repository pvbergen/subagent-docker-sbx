"""A bare sandbox for probing sbx networking, policy and secrets from inside a VM.

Used for U6–U9: most of those checks only need `curl` in a sandbox, not a full agent server, so
they skip the dependency install that makes `SandboxedAgentServer` slow to start.
"""

from __future__ import annotations

import atexit
import logging
import shutil
import tempfile
import uuid
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Literal

from deepagents_docker_sbx._sbx import (
    SBXRunResult,
    format_sbx_error,
    run_sbx,
    sbx_unavailable_reason,
)
from deepagents_docker_sbx.errors import SBXError

from .sandboxed_agent import (
    CLEANUP_TIMEOUT,
    CREATE_TIMEOUT,
    SANDBOX_AGENT,
    SBX_CHECK_TIMEOUT,
    kit_args,
)

logger = logging.getLogger(__name__)

# The published v3 shell workload (pulled, never pushed). v3 mixins only compose onto a v3
# workload; the built-in `shell` agent name selects the v2 kit, which rejects them.
SHELL_WORKLOAD_V3 = "docker/sbx-kit-shell:1.0.0"

EXEC_TIMEOUT = 60
CURL_MAX_TIME = 20
_BODY_LIMIT = 2000
_STATUS_MARKER = "__http_status__="

PolicyAction = Literal["allow", "deny"]


def build_probe_create_args(
    name: str,
    workspace: Path,
    *,
    workload: str = SANDBOX_AGENT,
    kits: Sequence[str | Path] = (),
) -> list[str]:
    """`sbx create` args for a bare sandbox with optional kits and no published ports."""
    return ["create", "--name", name, "--no-progress", *kit_args(kits), workload, str(workspace)]


def build_policy_args(action: PolicyAction, sandbox: str, resource: str) -> list[str]:
    """`sbx policy allow|deny network` args for a rule scoped to one sandbox."""
    return ["policy", action, "network", "--sandbox", sandbox, resource]


def build_secret_args(service: str, sandbox: str, token: str) -> list[str]:
    """`sbx secret set` args for a secret scoped to one sandbox. Never log these."""
    return ["secret", "set", service, "--sandbox", sandbox, "-t", token]


def build_curl_script(url: str, method: str = "GET", *, headers: Sequence[str] = ()) -> str:
    """Shell script printing the response body, then a marker line with the HTTP status.

    A proxy refusal is a 403 whose body says why; a connection failure prints status 000, with
    curl's error on stderr and its exit code as the script's.
    """
    header_args = "".join(f"-H '{header}' " for header in headers)
    # curl expands the `\n` in the -w format itself.
    return (
        f"curl -sS -X {method} --max-time {CURL_MAX_TIME} {header_args}"
        f"-w '\\n{_STATUS_MARKER}%{{http_code}}' '{url}'"
    )


@dataclass(frozen=True)
class HttpProbe:
    """Outcome of one HTTP request made from inside a sandbox."""

    status: int
    body: str
    # curl's exit code and error when no HTTP response came back (status 0).
    error: str = ""

    def describe(self) -> str:
        if self.error:
            return f"HTTP {self.status} ({self.error}): {self.body[:300].strip()}"
        return f"HTTP {self.status}: {self.body[:300].strip()}"


def parse_curl_output(stdout: str) -> HttpProbe:
    """Split `build_curl_script` output into status and (truncated) body."""
    body, marker, status = stdout.rpartition(f"\n{_STATUS_MARKER}")
    if not marker:
        return HttpProbe(status=0, body=stdout[:_BODY_LIMIT])
    try:
        code = int(status.strip())
    except ValueError:
        code = 0
    return HttpProbe(status=code, body=body[:_BODY_LIMIT])


def _atexit_close(ref: weakref.ReferenceType[ProbeSandbox]) -> None:
    sandbox = ref()
    if sandbox is not None:
        sandbox.close()


class ProbeSandbox:
    """A sandbox with an empty temp workspace, for running probes with `sbx exec`."""

    def __init__(
        self,
        *,
        workload: str = SANDBOX_AGENT,
        kits: Sequence[str | Path] = (),
    ) -> None:
        self._name = f"deepagents-probe-{uuid.uuid4().hex[:12]}"
        self._workload = workload
        self._workspace = Path(tempfile.mkdtemp(prefix=f"{self._name}-"))
        self._closed = False
        self._atexit_hook: Callable[[], None] = partial(_atexit_close, weakref.ref(self))
        atexit.register(self._atexit_hook)
        try:
            reason = sbx_unavailable_reason(timeout=SBX_CHECK_TIMEOUT)
            if reason is not None:
                msg = f"sbx is not available ({reason})"
                raise SBXError(msg)
            logger.info("creating probe sandbox %s (%s, kits=%s)", self._name, workload, kits)
            args = build_probe_create_args(
                self._name, self._workspace, workload=workload, kits=kits
            )
            self._run(args, timeout=CREATE_TIMEOUT, what="sbx create")
        except BaseException:
            self.close()
            raise

    @property
    def name(self) -> str:
        """Sandbox name as shown by `sbx ls`."""
        return self._name

    @property
    def workload(self) -> str:
        """Built-in agent name or v3 workload reference the sandbox was created from."""
        return self._workload

    def _run(self, args: list[str], *, timeout: float, what: str) -> SBXRunResult:
        result = run_sbx(args, timeout=timeout)
        if result.returncode != 0:
            msg = f"{what} failed: {format_sbx_error(result)}"
            raise SBXError(msg)
        return result

    def exec(self, script: str, *, timeout: float = EXEC_TIMEOUT) -> SBXRunResult:
        """Run `script` with `sh -c` inside the sandbox."""
        return run_sbx(["exec", self._name, "sh", "-c", script], timeout=timeout)

    def http(self, url: str, method: str = "GET", *, headers: Sequence[str] = ()) -> HttpProbe:
        """Make one HTTP request from inside the sandbox through the sbx proxy."""
        script = build_curl_script(url, method, headers=headers)
        result = self.exec(script, timeout=CURL_MAX_TIME + 30)
        probe = parse_curl_output(result.stdout)
        if probe.status == 0:
            error = f"curl exit {result.returncode}: {result.stderr.strip()}"
            probe = replace(probe, error=error)
        return probe

    def policy(self, action: PolicyAction, resource: str) -> None:
        """Add a network rule scoped to this sandbox."""
        args = build_policy_args(action, self._name, resource)
        self._run(args, timeout=SBX_CHECK_TIMEOUT, what=f"sbx policy {action} network")

    def set_secret(self, service: str, token: str) -> None:
        """Store a secret scoped to this sandbox; the proxy injects it, the VM never sees it."""
        result = run_sbx(build_secret_args(service, self._name, token), timeout=SBX_CHECK_TIMEOUT)
        if result.returncode != 0:
            # The error text is safe to show; the arguments, which hold the token, are not.
            msg = f"sbx secret set {service} failed: {format_sbx_error(result)}"
            raise SBXError(msg)

    def close(self) -> None:
        """Remove the sandbox and its temp workspace."""
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self._atexit_hook)
        try:
            run_sbx(["rm", "--force", self._name], timeout=CLEANUP_TIMEOUT)
        except SBXError:
            pass
        shutil.rmtree(self._workspace, ignore_errors=True)

    def __enter__(self) -> ProbeSandbox:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
