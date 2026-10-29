"""Tests for `subagents_docker_sbx`: a worker agent served from its own sbx sandbox.

The unit tests mock sbx. The integration tests boot a real sandbox and only run when opted in
with ``SUBAGENTS_SBX_INTEGRATION=1``; the supervisor round trip additionally needs
``SUBAGENTS_SBX_LLM=1`` and ``OPENAI_API_KEY`` on the host (stored as a sandbox-scoped secret).
``SUBAGENTS_SBX_TEMPLATE=<ref>`` also starts a worker from a template built with
``docker/worker.Dockerfile`` and loaded into sbx.
"""

import os
import re
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from subagents_docker_sbx import WORKER_REQUIREMENTS, SandboxedWorker, SBXError, WorkerSpec
from subagents_docker_sbx._sbx import SBXRunResult, run_sbx, sbx_unavailable_reason
from subagents_docker_sbx.requirements import VENV_DIR, WORKER_CODE_DIR
from subagents_docker_sbx.worker import (
    PROXY_MANAGED_VALUE,
    SANDBOX_PORT,
    build_create_args,
    build_install_args,
    build_serve_args,
)

WORKER_DIR = Path(__file__).resolve().parent / "workers" / "basic"
DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "worker.Dockerfile"


def _spec(**overrides: object) -> WorkerSpec:
    fields: dict[str, object] = {
        "name": "worker",
        "description": "General worker.",
        "worker_dir": WORKER_DIR,
        "graph_id": "worker",
    }
    fields.update(overrides)
    return WorkerSpec(**fields)  # type: ignore[arg-type]


# --- spec ------------------------------------------------------------------------------------


def test_spec_resolves_worker_dir() -> None:
    assert _spec(worker_dir=str(WORKER_DIR)).path == WORKER_DIR


def test_spec_rejects_directory_without_langgraph_json(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no langgraph.json"):
        _spec(worker_dir=tmp_path)


def test_spec_rejects_unknown_graph() -> None:
    with pytest.raises(ValueError, match="'nope' is not in"):
        _spec(graph_id="nope")


def test_spec_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="name"):
        _spec(name=" ")


def test_spec_without_worker_dir_needs_a_template() -> None:
    with pytest.raises(ValueError, match="needs a template"):
        _spec(worker_dir=None)


def test_spec_without_worker_dir_serves_code_from_the_image() -> None:
    spec = _spec(worker_dir=None, template="tpl:v1", graph_id="anything")

    assert spec.path is None
    assert spec.workdir == WORKER_CODE_DIR


def test_spec_rejects_env_that_overrides_a_secret_variable() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        _spec(secrets={"openai": "OPENAI_API_KEY"}, env={"OPENAI_API_KEY": "sk-real"})


# --- argument builders -----------------------------------------------------------------------


def test_create_args_publish_on_loopback_and_pass_options() -> None:
    spec = _spec(template="tpl:v1", memory="2g", cpus=2, kits=["docker/kit:1", WORKER_DIR])
    args = build_create_args(spec, "sb", 8123)

    assert args[args.index("--publish") + 1] == f"127.0.0.1:8123:{SANDBOX_PORT}"
    assert args[args.index("--template") + 1] == "tpl:v1"
    assert args[args.index("--memory") + 1] == "2g"
    assert args[args.index("--cpus") + 1] == "2"
    assert [args[i + 1] for i, a in enumerate(args) if a == "--kit"] == [
        "docker/kit:1",
        str(WORKER_DIR),
    ]
    assert args[-2:] == ["shell", str(WORKER_DIR)]


def test_create_args_leave_defaults_to_sbx() -> None:
    args = build_create_args(_spec(), "sb", 8123)

    for flag in ("--template", "--memory", "--cpus", "--kit"):
        assert flag not in args


def test_create_args_without_worker_dir_mount_nothing() -> None:
    args = build_create_args(_spec(worker_dir=None, template="tpl:v1"), "sb", 8123)

    assert args[-1] == "shell"
    assert args[args.index("--template") + 1] == "tpl:v1"


def test_serve_args_without_worker_dir_use_the_code_in_the_image() -> None:
    args = build_serve_args(_spec(worker_dir=None, template="tpl:v1"), "sb")

    assert args[args.index("--workdir") + 1] == WORKER_CODE_DIR


def test_install_args_install_worker_requirements_into_venv() -> None:
    args = build_install_args(_spec(), "sb")

    assert args[:4] == ["exec", "--workdir", str(WORKER_DIR), "sb"]
    assert args[4:6] == ["sh", "-c"]
    assert args[7:9] == ["sh", VENV_DIR]
    assert args[9:] == list(WORKER_REQUIREMENTS)


def test_serve_args_use_placeholders_for_secrets_and_pass_env() -> None:
    spec = _spec(secrets={"openai": "OPENAI_API_KEY"}, env={"WORKER_MODEL": "openai:gpt-5.5"})
    args = build_serve_args(spec, "sb")

    env = [args[i + 1] for i, a in enumerate(args) if a == "--env"]
    assert env == [f"OPENAI_API_KEY={PROXY_MANAGED_VALUE}", "WORKER_MODEL=openai:gpt-5.5"]
    assert args[args.index("sb") + 1] == f"{VENV_DIR}/bin/langgraph"
    assert args[args.index("--port") + 1] == str(SANDBOX_PORT)


def test_worker_requirements_include_langgraph_dev_runtime() -> None:
    names = {req.split(">")[0].split("=")[0] for req in WORKER_REQUIREMENTS}

    assert names == {"deepagents", "langgraph-cli[inmem]"}


def test_dockerfile_installs_worker_requirements_into_the_venv() -> None:
    text = DOCKERFILE.read_text()
    requirements = re.search(r'^ARG WORKER_REQUIREMENTS="([^"]*)"$', text, re.MULTILINE)

    assert requirements is not None
    assert tuple(requirements.group(1).split()) == WORKER_REQUIREMENTS
    assert f'uv venv --python "$python" {VENV_DIR}' in text
    assert f"--python {VENV_DIR}/bin/python ${{WORKER_REQUIREMENTS}}" in text


def test_dockerfile_copies_the_worker_code_into_the_image() -> None:
    lines = DOCKERFILE.read_text().splitlines()

    assert f"COPY --chown=agent:agent . {WORKER_CODE_DIR}" in lines


# --- lifecycle (sbx mocked) ------------------------------------------------------------------


def _ok(*_args: object, **_kwargs: object) -> SBXRunResult:
    return SBXRunResult(returncode=0, stdout="", stderr="")


@pytest.fixture
def sbx() -> Iterator[MagicMock]:
    """Mock sbx calls, the server process and the readiness check; yield the run_sbx mock."""
    with (
        patch("subagents_docker_sbx.worker.run_sbx", side_effect=_ok) as run,
        patch("subagents_docker_sbx.worker.sbx_unavailable_reason", return_value=None),
        patch("subagents_docker_sbx.worker.subprocess.Popen") as popen,
        patch.object(SandboxedWorker, "_wait_until_ready"),
    ):
        popen.return_value.poll.return_value = None
        yield run


def _commands(run: MagicMock) -> list[list[str]]:
    return [call.args[0] for call in run.call_args_list]


def test_start_order_create_secrets_rules_install(
    sbx: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    spec = _spec(
        secrets={"openai": "OPENAI_API_KEY"},
        allow_network=["api.tavily.com"],
        deny_network=["pypi.org"],
    )

    with SandboxedWorker(spec) as worker:
        pass
    commands = _commands(sbx)

    assert [c[0] for c in commands[:5]] == ["create", "secret", "policy", "policy", "exec"]
    assert commands[1] == ["secret", "set", "openai", "--sandbox", worker.name, "-t", "sk-test"]
    assert commands[2] == ["policy", "allow", "network", "--sandbox", worker.name, "api.tavily.com"]
    assert commands[3] == ["policy", "deny", "network", "--sandbox", worker.name, "pypi.org"]
    assert commands[-1] == ["rm", "--force", worker.name]


def test_worker_without_worker_dir_never_installs(sbx: MagicMock) -> None:
    with SandboxedWorker(_spec(worker_dir=None, template="tpl:v1")):
        pass
    commands = _commands(sbx)

    assert commands[0][-1] == "shell"
    assert not [c for c in commands if c[0] == "exec"]


def test_template_skips_install(sbx: MagicMock) -> None:
    with SandboxedWorker(_spec(template="tpl:v1")):
        commands = _commands(sbx)

    assert not [c for c in commands if c[0] == "exec"]


def test_missing_secret_variable_fails_before_create(
    sbx: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)

    with pytest.raises(ValueError, match="MISSING_KEY"):
        SandboxedWorker(_spec(secrets={"openai": "MISSING_KEY"}))

    assert sbx.call_count == 0


def test_failed_step_removes_the_sandbox(sbx: MagicMock) -> None:
    def fail_policy(args: list[str], **_kwargs: object) -> SBXRunResult:
        if args[0] == "policy":
            return SBXRunResult(returncode=1, stdout="", stderr="bad rule")
        return _ok()

    sbx.side_effect = fail_policy

    with pytest.raises(SBXError, match="bad rule"):
        SandboxedWorker(_spec(allow_network=["x"]))

    assert _commands(sbx)[-1][:2] == ["rm", "--force"]


def test_close_is_idempotent(sbx: MagicMock) -> None:
    worker = SandboxedWorker(_spec(template="tpl:v1"))
    worker.close()
    worker.close()

    assert [c[0] for c in _commands(sbx)].count("rm") == 1


def test_subagent_points_at_the_published_port(sbx: MagicMock) -> None:
    with SandboxedWorker(_spec(template="tpl:v1", host_port=8123)) as worker:
        subagent = worker.subagent()

    assert subagent == {
        "name": "worker",
        "description": "General worker.",
        "graph_id": "worker",
        "url": "http://127.0.0.1:8123",
    }


def test_sandbox_name_is_derived_from_worker_name(sbx: MagicMock) -> None:
    with SandboxedWorker(_spec(name="My Researcher!", template="tpl:v1")) as worker:
        assert worker.name.startswith("subagent-my-researcher-")


# --- integration -----------------------------------------------------------------------------


def _skip_reason() -> str | None:
    if os.environ.get("SUBAGENTS_SBX_INTEGRATION") != "1":
        return "set SUBAGENTS_SBX_INTEGRATION=1 to run sbx integration tests"
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
def worker() -> Iterator[SandboxedWorker]:
    # The scoped secret is only set when the key is available; the server starts either way.
    secrets = {"openai": "OPENAI_API_KEY"} if os.environ.get("OPENAI_API_KEY") else {}
    started = SandboxedWorker(_spec(secrets=secrets))
    try:
        yield started
    finally:
        started.close()


@integration
def test_worker_answers_health_check(worker: SandboxedWorker) -> None:
    with urllib.request.urlopen(f"{worker.url}/ok", timeout=10) as response:  # noqa: S310
        assert response.status == 200


@integration
def test_worker_graph_is_registered(worker: SandboxedWorker) -> None:
    from langgraph_sdk import get_sync_client

    assistants = get_sync_client(url=worker.url).assistants.search()

    assert "worker" in {assistant["graph_id"] for assistant in assistants}


@integration
def test_worker_port_is_published_on_host_loopback_only(worker: SandboxedWorker) -> None:
    ports = run_sbx(["ports", worker.name], timeout=30)

    assert ports.returncode == 0, ports.stderr
    assert "127.0.0.1" in ports.stdout
    assert "0.0.0.0" not in ports.stdout


@integration
@pytest.mark.skipif(
    os.environ.get("SUBAGENTS_SBX_LLM") != "1" or not os.environ.get("OPENAI_API_KEY"),
    reason="set SUBAGENTS_SBX_LLM=1 and OPENAI_API_KEY to call the model",
)
def test_host_supervisor_delegates_to_sandboxed_worker(worker: SandboxedWorker) -> None:
    # The supervisor's model runs on the host, so its provider package must be installed there
    # too (the worker's runs in the sandbox, from the worker's requirements.txt).
    pytest.importorskip(
        "langchain_openai",
        reason="the host-side supervisor needs langchain-openai: uv sync --group integration",
    )
    from deepagents import create_deep_agent

    supervisor = create_deep_agent(
        model="openai:gpt-5.5",
        system_prompt=(
            "You coordinate work. Delegate the user's task to the `worker` async subagent, "
            "then check on the task until it has finished and report its result."
        ),
        subagents=[worker.subagent()],
    )
    result = supervisor.invoke(
        {"messages": [{"role": "user", "content": "Name three prime numbers above 100."}]},
    )
    answer = result["messages"][-1].content
    print(f"supervisor -> {answer}")

    assert answer


@integration
@pytest.mark.skipif(
    not os.environ.get("SUBAGENTS_SBX_TEMPLATE"),
    reason="set SUBAGENTS_SBX_TEMPLATE to a template built from docker/worker.Dockerfile",
)
def test_worker_starts_from_dockerfile_template() -> None:
    template = os.environ["SUBAGENTS_SBX_TEMPLATE"]

    with SandboxedWorker(_spec(template=template)) as templated:
        with urllib.request.urlopen(f"{templated.url}/ok", timeout=10) as response:  # noqa: S310
            assert response.status == 200


@integration
@pytest.mark.skipif(
    not os.environ.get("SUBAGENTS_SBX_TEMPLATE"),
    reason="set SUBAGENTS_SBX_TEMPLATE to a template built from docker/worker.Dockerfile",
)
def test_worker_runs_from_template_without_a_host_directory() -> None:
    from langgraph_sdk import get_sync_client

    template = os.environ["SUBAGENTS_SBX_TEMPLATE"]

    with SandboxedWorker(_spec(worker_dir=None, template=template)) as imaged:
        assistants = get_sync_client(url=imaged.url).assistants.search()
        listing = run_sbx(["ls"], timeout=30).stdout
        print(f"sbx ls with an unmounted worker:\n{listing}")

        assert "worker" in {assistant["graph_id"] for assistant in assistants}
