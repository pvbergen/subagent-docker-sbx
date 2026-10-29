"""U6–U9 for the agent-in-sandbox prototype: networking, templates, scoped secrets and egress.

The argument-builder and kit tests always run. The rest boot real sandboxes and only run when
opted in with ``DEEPAGENTS_SBX_INTEGRATION=1``. U8 additionally needs
``DEEPAGENTS_SBX_SCOPED_SECRET=1``, ``OPENAI_API_KEY`` on the host and **no** global
``openai`` secret (``sbx secret rm openai``), so that only the sandbox-scoped one can inject.

Run with ``-s`` to see the recorded observations for the README.
"""

import hashlib
import os
import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from deepagents_docker_sbx._sbx import run_sbx, sbx_unavailable_reason
from prototypes.agent_in_sbx.probe_sandbox import (
    SHELL_WORKLOAD_V3,
    HttpProbe,
    ProbeSandbox,
    build_curl_script,
    build_policy_args,
    build_probe_create_args,
    build_secret_args,
    parse_curl_output,
)
from prototypes.agent_in_sbx.sandboxed_agent import (
    PROXY_MANAGED_KEY,
    SandboxedAgentServer,
    build_create_args,
    build_template_save_args,
)

PROTOTYPE_DIR = Path(__file__).resolve().parent.parent / "prototypes" / "agent_in_sbx"
WORKER_DIR = PROTOTYPE_DIR / "worker"
KITS_DIR = PROTOTYPE_DIR / "kits"

ALLOW_PROBE = KITS_DIR / "allow-probe"
DENY_PROBE = KITS_DIR / "deny-probe"
DENY_ALL_ALLOW_PROBE = KITS_DIR / "deny-all-allow-probe"
GET_ONLY_PROBE = KITS_DIR / "get-only-probe"

# Not in the global `balanced` policy; `/get` answers GET only, for the bounded-allow check.
PROBE_HOST = "httpbin.org"
PROBE_URL = f"https://{PROBE_HOST}/get"
# In the global `balanced` policy (U1 installs dependencies from it).
ALLOWED_HOST = "pypi.org"
ALLOWED_URL = f"https://{ALLOWED_HOST}/simple/"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
WORKLOADS = ["shell", SHELL_WORKLOAD_V3]


# --- argument builders and kits (always run) -------------------------------------------------


def test_create_args_add_kits_template_and_agent() -> None:
    args = build_create_args(
        "sb",
        WORKER_DIR,
        8123,
        agent=SHELL_WORKLOAD_V3,
        kits=[ALLOW_PROBE, "docker/some-kit:1.0.0"],
        template="tpl:v1",
    )

    assert args[args.index("--template") + 1] == "tpl:v1"
    kits = [args[i + 1] for i, arg in enumerate(args) if arg == "--kit"]
    assert kits == [str(ALLOW_PROBE), "docker/some-kit:1.0.0"]
    assert args[-2:] == [SHELL_WORKLOAD_V3, str(WORKER_DIR)]


def test_create_args_default_to_plain_shell_sandbox() -> None:
    args = build_create_args("sb", WORKER_DIR, 8123)

    assert "--kit" not in args
    assert "--template" not in args
    assert args[-2:] == ["shell", str(WORKER_DIR)]


def test_template_save_args() -> None:
    assert build_template_save_args("sb", "tpl:v1") == ["template", "save", "sb", "tpl:v1"]


def test_probe_create_args_publish_no_ports(tmp_path: Path) -> None:
    args = build_probe_create_args("pb", tmp_path, workload=SHELL_WORKLOAD_V3, kits=[DENY_PROBE])

    assert "--publish" not in args
    assert args[args.index("--kit") + 1] == str(DENY_PROBE)
    assert args[-2:] == [SHELL_WORKLOAD_V3, str(tmp_path)]


def test_policy_args_are_scoped_to_one_sandbox() -> None:
    assert build_policy_args("deny", "pb", "pypi.org") == [
        "policy",
        "deny",
        "network",
        "--sandbox",
        "pb",
        "pypi.org",
    ]


def test_secret_args_are_scoped_to_one_sandbox() -> None:
    args = build_secret_args("openai", "pb", "tok")

    assert args[:3] == ["secret", "set", "openai"]
    assert args[args.index("--sandbox") + 1] == "pb"
    assert args[args.index("-t") + 1] == "tok"


def test_curl_script_reports_status_after_body() -> None:
    script = build_curl_script("https://example.com/", "POST", headers=["X-A: b"])

    assert script.startswith("curl -sS -X POST ")
    assert "-H 'X-A: b'" in script
    assert "%{http_code}" in script
    assert script.endswith("'https://example.com/'")


def test_parse_curl_output() -> None:
    assert parse_curl_output("Blocked by policy\n__http_status__=403") == HttpProbe(
        status=403, body="Blocked by policy"
    )
    assert parse_curl_output("\n__http_status__=000").status == 0
    assert parse_curl_output("no marker").status == 0


def test_describe_includes_curl_error() -> None:
    probe = HttpProbe(status=0, body="", error="curl exit 28: timed out")

    assert probe.describe() == "HTTP 0 (curl exit 28: timed out): "


@pytest.mark.parametrize(
    "kit", [ALLOW_PROBE, DENY_PROBE, DENY_ALL_ALLOW_PROBE, GET_ONLY_PROBE], ids=lambda p: p.name
)
def test_probe_kits_are_v3_declaration_only_mixins(kit: Path) -> None:
    descriptor_path = kit / f"{kit.name}.yaml"
    text = descriptor_path.read_text()
    descriptor = yaml.safe_load(text)

    assert text.startswith("# syntax=docker/sandbox-kit:3\n")
    assert descriptor["schemaVersion"] == "3"
    assert descriptor["kind"] == "mixin"
    assert "build" not in descriptor and "dockerfile" not in descriptor
    [policy] = descriptor["capabilities"]
    assert policy["type"] in {
        "com.docker.sandbox/network-policy@1",
        "com.docker.sandbox/network-policy@2",
    }
    assert set(policy["config"]) == {"runtime"}


# --- integration -----------------------------------------------------------------------------


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


def _record(question: str, what: str, probe: HttpProbe | str) -> None:
    detail = probe.describe() if isinstance(probe, HttpProbe) else probe
    print(f"[{question}] {what}: {detail}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def assert_blocked(probe: HttpProbe, what: str) -> None:
    assert probe.status == 403, f"{what}: expected a proxy 403, got {probe.describe()}"


def assert_reachable(probe: HttpProbe, what: str) -> None:
    assert probe.status not in (0, 403), f"{what}: expected to get through, got {probe.describe()}"


@pytest.fixture(scope="module", params=WORKLOADS)
def plain(request: pytest.FixtureRequest) -> Iterator[ProbeSandbox]:
    """A sandbox without kits or per-sandbox rules: the baseline for each workload."""
    sandbox = ProbeSandbox(workload=request.param)
    try:
        yield sandbox
    finally:
        sandbox.close()


@pytest.fixture(scope="module")
def plain_v3() -> Iterator[ProbeSandbox]:
    sandbox = ProbeSandbox(workload=SHELL_WORKLOAD_V3)
    try:
        yield sandbox
    finally:
        sandbox.close()


# U6: sandbox to sandbox through a published port.


@integration
def test_u6_sandbox_reaches_other_sandbox_published_port() -> None:
    port = _free_port()
    url = f"http://host.docker.internal:{port}/ok"
    with (
        SandboxedAgentServer(WORKER_DIR, host_port=port) as target,
        ProbeSandbox() as caller,
        ProbeSandbox() as bystander,
    ):
        _record("U6", f"target {target.name} published on 127.0.0.1:{port}", target.url)
        before = caller.http(url)
        _record("U6", "caller, no rule", before)

        caller.policy("allow", f"localhost:{port}")
        after = caller.http(url)
        _record("U6", f"caller, after allow localhost:{port}", after)
        other = bystander.http(url)
        _record("U6", "bystander without the rule", other)

        assert after.status == 200, after.describe()
        assert other.status != 200, f"rule leaked to another sandbox: {other.describe()}"


# U7: a saved template skips bootstrap.sh.


@integration
def test_u7_template_skips_bootstrap() -> None:
    digest = hashlib.sha256((WORKER_DIR / "requirements.txt").read_bytes()).hexdigest()[:12]
    template = f"deepagents-proto-worker:{digest}"
    try:
        start = time.monotonic()
        with SandboxedAgentServer(WORKER_DIR, host_port=_free_port()) as first:
            bootstrapped = time.monotonic() - start
            first.save_template(template)
        _record("U7", "start with bootstrap.sh", f"{bootstrapped:.1f}s")

        start = time.monotonic()
        with SandboxedAgentServer(WORKER_DIR, host_port=_free_port(), template=template) as second:
            from_template = time.monotonic() - start
            venv_langgraph = "/home/agent/venv/bin/langgraph"
            ok = run_sbx(["exec", second.name, "test", "-x", venv_langgraph], timeout=30)
        _record("U7", f"start from template {template}", f"{from_template:.1f}s")

        assert ok.returncode == 0, "venv missing in the sandbox created from the template"
        assert from_template < bootstrapped
    finally:
        # Local image store only; nothing is pushed anywhere. `template rm` asks for
        # confirmation on stdin and cancels when it reads nothing.
        removed = run_sbx(["template", "rm", template], timeout=60, input_text="y\n")
        _record("U7", "template rm", f"exit {removed.returncode} {removed.stderr.strip()}")


# U8: a secret scoped to one sandbox, with no global secret.


@pytest.fixture
def openai_key() -> str:
    if os.environ.get("DEEPAGENTS_SBX_SCOPED_SECRET") != "1":
        pytest.skip("set DEEPAGENTS_SBX_SCOPED_SECRET=1 after `sbx secret rm openai` to run U8")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        pytest.skip("U8 needs OPENAI_API_KEY on the host to store as a sandbox-scoped secret")
    return key


@integration
def test_u8_secret_scoped_to_one_sandbox(openai_key: str) -> None:
    # The OpenAI SDK sends the placeholder as a bearer token; the proxy swaps in the secret.
    headers = [f"Authorization: Bearer {PROXY_MANAGED_KEY}"]
    with ProbeSandbox() as scoped, ProbeSandbox() as unscoped:
        scoped.set_secret("openai", openai_key)
        with_secret = scoped.http(OPENAI_MODELS_URL, headers=headers)
        without_secret = unscoped.http(OPENAI_MODELS_URL, headers=headers)
        _record("U8", "sandbox with scoped secret", HttpProbe(with_secret.status, ""))
        _record("U8", "sandbox without secret", HttpProbe(without_secret.status, ""))

    assert with_secret.status == 200, f"scoped secret not injected: HTTP {with_secret.status}"
    assert without_secret.status == 401, (
        f"expected 401 without a secret, got HTTP {without_secret.status} "
        "(is a global `openai` secret still set?)"
    )


@integration
@pytest.mark.skipif(
    os.environ.get("DEEPAGENTS_SBX_LLM") != "1",
    reason="set DEEPAGENTS_SBX_LLM=1 to call the model",
)
def test_u8_worker_round_trip_with_scoped_secret_only(openai_key: str) -> None:
    from prototypes.agent_in_sbx.run_supervisor import check_worker_directly

    with SandboxedAgentServer(WORKER_DIR, host_port=_free_port()) as server:
        result = run_sbx(build_secret_args("openai", server.name, openai_key), timeout=30)
        assert result.returncode == 0, "sbx secret set --sandbox failed"

        assert check_worker_directly(server.url).strip() != ""


# U9: per-sandbox egress rules, set explicitly with `sbx policy` ...


@integration
def test_u9_explicit_allow_is_scoped_to_one_sandbox(plain: ProbeSandbox) -> None:
    workload = plain.workload
    with ProbeSandbox(workload=workload) as sandbox:
        assert_blocked(sandbox.http(PROBE_URL), f"{PROBE_HOST} before the rule ({workload})")

        sandbox.policy("allow", PROBE_HOST)
        allowed = sandbox.http(PROBE_URL)
        baseline = plain.http(PROBE_URL)
        _record("U9", f"explicit allow {PROBE_HOST} ({workload})", allowed)
        _record("U9", f"same host, sandbox without the rule ({workload})", baseline)

        assert_reachable(allowed, f"{PROBE_HOST} after `sbx policy allow`")
        assert_blocked(baseline, f"{PROBE_HOST} in a sandbox without the rule")


@integration
def test_u9_explicit_deny_narrows_one_sandbox(plain: ProbeSandbox) -> None:
    workload = plain.workload
    with ProbeSandbox(workload=workload) as sandbox:
        assert_reachable(sandbox.http(ALLOWED_URL), f"{ALLOWED_HOST} before the rule ({workload})")

        sandbox.policy("deny", ALLOWED_HOST)
        denied = sandbox.http(ALLOWED_URL)
        baseline = plain.http(ALLOWED_URL)
        _record("U9", f"explicit deny {ALLOWED_HOST} ({workload})", denied)

        assert_blocked(denied, f"{ALLOWED_HOST} after `sbx policy deny`")
        assert_reachable(baseline, f"{ALLOWED_HOST} in a sandbox without the rule")


@integration
def test_u9_explicit_deny_all_wins_over_allow(plain: ProbeSandbox) -> None:
    with ProbeSandbox(workload=plain.workload) as sandbox:
        sandbox.policy("allow", PROBE_HOST)
        sandbox.policy("deny", "**")
        probe = sandbox.http(PROBE_URL)
        _record("U9", f"explicit deny ** + allow {PROBE_HOST} ({plain.workload})", probe)

        assert_blocked(probe, f"{PROBE_HOST} with deny ** and an allow")
        assert_blocked(sandbox.http(ALLOWED_URL), f"{ALLOWED_HOST} with deny **")


@integration
def test_u9_policy_rules_are_removed_with_the_sandbox() -> None:
    sandbox = ProbeSandbox()
    name = sandbox.name
    try:
        sandbox.policy("allow", PROBE_HOST)
        assert name in run_sbx(["policy", "ls"], timeout=30).stdout
    finally:
        sandbox.close()

    rules = run_sbx(["policy", "ls"], timeout=30).stdout.splitlines()
    leftover = [line for line in rules if name in line]
    _record("U9", "rules left after sbx rm", "; ".join(leftover) or "none")
    assert not leftover


# ... and declared by v3 kits.


@integration
def test_u9_kit_allow_widens_egress(plain_v3: ProbeSandbox) -> None:
    with ProbeSandbox(workload=SHELL_WORKLOAD_V3, kits=[ALLOW_PROBE]) as sandbox:
        allowed = sandbox.http(PROBE_URL)
        baseline = plain_v3.http(PROBE_URL)
        _record("U9", f"kit allow {PROBE_HOST}", allowed)
        _record("U9", "same host, sandbox without the kit", baseline)

        assert_reachable(allowed, f"{PROBE_HOST} with the allow-probe kit")
        assert_blocked(baseline, f"{PROBE_HOST} without the kit")


@integration
def test_u9_kit_deny_narrows_egress(plain_v3: ProbeSandbox) -> None:
    with ProbeSandbox(workload=SHELL_WORKLOAD_V3, kits=[DENY_PROBE]) as sandbox:
        denied = sandbox.http(ALLOWED_URL)
        baseline = plain_v3.http(ALLOWED_URL)
        _record("U9", f"kit deny {ALLOWED_HOST}", denied)
        _record("U9", "same host, sandbox without the kit", baseline)

        assert_blocked(denied, f"{ALLOWED_HOST} with the deny-probe kit")
        assert_reachable(baseline, f"{ALLOWED_HOST} without the kit")


@integration
def test_u9_kit_deny_all_wins_over_allow() -> None:
    with ProbeSandbox(workload=SHELL_WORKLOAD_V3, kits=[DENY_ALL_ALLOW_PROBE]) as sandbox:
        probe = sandbox.http(PROBE_URL)
        _record("U9", f"kit deny ** + allow {PROBE_HOST}", probe)

        assert_blocked(probe, f"{PROBE_HOST} with deny ** and an allow in the kit")
        assert_blocked(sandbox.http(ALLOWED_URL), f"{ALLOWED_HOST} with deny ** in the kit")


@integration
def test_u9_kit_bounded_allow_refuses_other_methods() -> None:
    with ProbeSandbox(workload=SHELL_WORKLOAD_V3, kits=[GET_ONLY_PROBE]) as sandbox:
        get = sandbox.http(PROBE_URL)
        post = sandbox.http(PROBE_URL, "POST")
        _record("U9", f"kit GET-only {PROBE_HOST}: GET", get)
        _record("U9", f"kit GET-only {PROBE_HOST}: POST", post)

        assert_reachable(get, f"GET {PROBE_HOST} with the GET-only kit")
        assert_blocked(post, f"POST {PROBE_HOST} with the GET-only kit")
