"""Tests for the agent-in-sandbox prototype (prototypes/agent_in_sbx).

The argument-builder tests always run. The rest boot a real sandbox and only run when opted in
with ``DEEPAGENTS_SBX_INTEGRATION=1``; the model round trip additionally needs
``DEEPAGENTS_SBX_LLM=1`` and ``sbx secret set openai``.
"""

import os
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from deepagents_docker_sbx._sbx import run_sbx, sbx_unavailable_reason
from prototypes.agent_in_sbx.sandboxed_agent import (
    PROXY_MANAGED_KEY,
    SANDBOX_PORT,
    SandboxedAgentServer,
    build_create_args,
    build_install_args,
    build_serve_args,
)

WORKER_DIR = Path(__file__).resolve().parent.parent / "prototypes" / "agent_in_sbx" / "worker"


def test_create_args_publish_port_on_host_loopback_only() -> None:
    args = build_create_args("sb", WORKER_DIR, 8123)

    assert args[:2] == ["create", "--name"]
    assert args[args.index("--publish") + 1] == f"127.0.0.1:8123:{SANDBOX_PORT}"
    assert args[-2:] == ["shell", str(WORKER_DIR)]


def test_install_args_run_bootstrap_in_worker_dir() -> None:
    args = build_install_args("sb", WORKER_DIR)

    assert args == ["exec", "--workdir", str(WORKER_DIR), "sb", "sh", "./bootstrap.sh"]


def test_serve_args_use_placeholder_key_and_listen_on_sandbox_port() -> None:
    args = build_serve_args("sb", WORKER_DIR)

    assert args[args.index("--env") + 1] == f"OPENAI_API_KEY={PROXY_MANAGED_KEY}"
    assert args[args.index("--workdir") + 1] == str(WORKER_DIR)
    assert args[args.index("--host") + 1] == "0.0.0.0"
    assert args[args.index("--port") + 1] == str(SANDBOX_PORT)
    assert args[args.index("sb") + 1].endswith("/bin/langgraph")


def test_rejects_directory_without_langgraph_json(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no langgraph.json"):
        SandboxedAgentServer(tmp_path)


def _skip_reason() -> str | None:
    if os.environ.get("DEEPAGENTS_SBX_INTEGRATION") != "1":
        return "set DEEPAGENTS_SBX_INTEGRATION=1 to run sbx integration tests"
    reason = sbx_unavailable_reason(timeout=30)
    if reason is not None:
        return f"sbx is not available: {reason}"
    return None


_SKIP_REASON = _skip_reason()


def integration[F: Callable[..., object]](test: F) -> F:
    """Mark a test as an opt-in sbx integration test."""
    test = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")(test)
    return pytest.mark.sbx(test)


@pytest.fixture(scope="module")
def server() -> Iterator[SandboxedAgentServer]:
    backend = SandboxedAgentServer(WORKER_DIR)
    try:
        yield backend
    finally:
        backend.close()


@integration
def test_server_answers_health_check(server: SandboxedAgentServer) -> None:
    with urllib.request.urlopen(f"{server.url}/ok", timeout=10) as response:  # noqa: S310
        assert response.status == 200


@integration
def test_worker_graph_is_registered(server: SandboxedAgentServer) -> None:
    from langgraph_sdk import get_sync_client

    assistants = get_sync_client(url=server.url).assistants.search()

    assert "worker" in {assistant["graph_id"] for assistant in assistants}


@integration
def test_port_is_published_on_host_loopback_only(server: SandboxedAgentServer) -> None:
    ports = run_sbx(["ports", server.name], timeout=30)

    assert ports.returncode == 0, ports.stderr
    assert "127.0.0.1" in ports.stdout
    assert "0.0.0.0" not in ports.stdout


@integration
def test_real_openai_key_never_enters_the_sandbox(server: SandboxedAgentServer) -> None:
    # Collect every OPENAI_API_KEY value visible to any process in the sandbox, including the
    # running langgraph server.
    script = (
        "cat /proc/[0-9]*/environ 2>/dev/null | tr '\\0' '\\n' "
        "| sed -n 's/^OPENAI_API_KEY=//p' | sort -u"
    )
    result = run_sbx(["exec", server.name, "sh", "-c", script], timeout=30)
    values = set(result.stdout.split())

    assert PROXY_MANAGED_KEY in values
    host_key = os.environ.get("OPENAI_API_KEY")
    if host_key:
        # Compare without putting the key in the assertion message.
        assert not any(value == host_key for value in values), "real key found in sandbox"


@integration
@pytest.mark.skipif(
    os.environ.get("DEEPAGENTS_SBX_LLM") != "1",
    reason="set DEEPAGENTS_SBX_LLM=1 (and `sbx secret set openai`) to call the model",
)
def test_worker_round_trip_through_proxy_injected_key(server: SandboxedAgentServer) -> None:
    from prototypes.agent_in_sbx.run_supervisor import check_worker_directly

    assert check_worker_directly(server.url).strip() != ""


@integration
def test_close_removes_sandbox_and_port() -> None:
    standalone = SandboxedAgentServer(WORKER_DIR, host_port=SANDBOX_PORT + 1)
    name, url = standalone.name, standalone.url

    standalone.close()

    assert name not in run_sbx(["ls", "-q"], timeout=30).stdout.split()
    with pytest.raises((urllib.error.URLError, OSError)):
        urllib.request.urlopen(f"{url}/ok", timeout=5)  # noqa: S310


def test_text_extracts_plain_text_from_content_blocks() -> None:
    from prototypes.agent_in_sbx.run_supervisor import _text

    blocks = [{"type": "text", "text": "101, "}, {"type": "text", "text": "103"}, {"id": "x"}]

    assert _text(blocks) == "101, 103"
    assert _text("plain") == "plain"
