import subprocess
from unittest.mock import MagicMock, patch

import pytest

from subagents_docker_sbx._sbx import (
    SBX_BINARY,
    SBXRunResult,
    format_sbx_error,
    run_sbx,
    sbx_available,
    sbx_unavailable_reason,
)
from subagents_docker_sbx.errors import SBXError


def test_format_sbx_error_returns_plain_stderr() -> None:
    result = SBXRunResult(returncode=1, stdout="", stderr="image not found")
    assert format_sbx_error(result) == "image not found"


def test_format_sbx_error_extracts_json_message() -> None:
    result = SBXRunResult(
        returncode=1,
        stdout="",
        stderr='{"message": "Conflict. The container name is already in use."}',
    )
    assert format_sbx_error(result) == "Conflict. The container name is already in use."


def test_format_sbx_error_returns_json_without_message_field() -> None:
    payload = '{"errorDetail":{"code":404}}'
    result = SBXRunResult(returncode=1, stdout="", stderr=payload)
    assert format_sbx_error(result) == payload


def test_format_sbx_error_falls_back_to_exit_code() -> None:
    result = SBXRunResult(returncode=7, stdout="", stderr="")
    assert format_sbx_error(result) == "exit code 7"


@patch("subagents_docker_sbx._sbx.run_sbx")
def test_sbx_available_true_when_ls_succeeds(run_sbx: MagicMock) -> None:
    run_sbx.return_value = SBXRunResult(returncode=0, stdout="my-sandbox\n", stderr="")
    assert sbx_available() is True


@patch("subagents_docker_sbx._sbx.run_sbx")
def test_sbx_available_false_when_ls_fails(run_sbx: MagicMock) -> None:
    run_sbx.return_value = SBXRunResult(returncode=1, stdout="", stderr="daemon down")
    assert sbx_available() is False


@patch("subagents_docker_sbx._sbx.run_sbx")
def test_sbx_available_probes_with_ls(run_sbx: MagicMock) -> None:
    run_sbx.return_value = SBXRunResult(returncode=0, stdout="", stderr="")
    sbx_available(timeout=3)
    run_sbx.assert_called_once_with(["ls", "-q"], timeout=3)


@patch("subagents_docker_sbx._sbx.run_sbx")
def test_sbx_unavailable_reason_reports_login_error(run_sbx: MagicMock) -> None:
    run_sbx.return_value = SBXRunResult(
        returncode=1,
        stdout="",
        stderr="error: not signed in to Docker\n  try: sbx login\n",
    )
    reason = sbx_unavailable_reason()
    assert reason is not None
    assert "not signed in" in reason


@patch("subagents_docker_sbx._sbx.run_sbx")
def test_sbx_unavailable_reason_reports_missing_binary(run_sbx: MagicMock) -> None:
    run_sbx.side_effect = SBXError("sbx executable not found on PATH")
    assert sbx_unavailable_reason() == "sbx executable not found on PATH"


@patch("subagents_docker_sbx._sbx.run_sbx")
def test_sbx_unavailable_reason_is_none_when_ready(run_sbx: MagicMock) -> None:
    run_sbx.return_value = SBXRunResult(returncode=0, stdout="", stderr="")
    assert sbx_unavailable_reason() is None


@patch("subagents_docker_sbx._sbx.subprocess.run")
def test_run_sbx_invokes_sbx_binary(subprocess_run: MagicMock) -> None:
    completed = MagicMock()
    completed.returncode = 0
    completed.stdout = ""
    completed.stderr = ""
    subprocess_run.return_value = completed

    run_sbx(["ls", "-q"])

    assert subprocess_run.call_args[0][0] == [SBX_BINARY, "ls", "-q"]
    assert SBX_BINARY == "sbx"


@patch("subagents_docker_sbx._sbx.subprocess.run")
def test_run_sbx_returns_captured_output(subprocess_run: MagicMock) -> None:
    completed = MagicMock()
    completed.returncode = 0
    completed.stdout = "ok\n"
    completed.stderr = ""
    subprocess_run.return_value = completed

    result = run_sbx(["info"])

    assert result == SBXRunResult(returncode=0, stdout="ok\n", stderr="")
    subprocess_run.assert_called_once()


@patch("subagents_docker_sbx._sbx.subprocess.run")
def test_run_sbx_raises_on_timeout(subprocess_run: MagicMock) -> None:
    subprocess_run.side_effect = subprocess.TimeoutExpired(cmd="sbx", timeout=5)

    with pytest.raises(SBXError, match="timed out after 5 seconds"):
        run_sbx(["exec", "cid", "true"], timeout=5)


@patch("subagents_docker_sbx._sbx.subprocess.run")
def test_run_sbx_raises_when_sbx_missing(subprocess_run: MagicMock) -> None:
    subprocess_run.side_effect = FileNotFoundError

    with pytest.raises(SBXError, match="not found on PATH"):
        run_sbx(["info"])


def _fake_sbx_script(body: str) -> list[str]:
    """Build args that make the patched `sbx` binary run a python snippet."""
    return ["-c", body]


@patch("subagents_docker_sbx._sbx.subprocess.Popen")
def test_run_sbx_capped_raises_when_sbx_missing(popen: MagicMock) -> None:
    popen.side_effect = FileNotFoundError

    with pytest.raises(SBXError, match="not found on PATH"):
        run_sbx(["info"], max_output_bytes=1024)


def test_run_sbx_capped_returns_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "subagents_docker_sbx._sbx.subprocess.Popen",
        _popen_shim(["sh", "-c", "printf hello; printf oops >&2"]),
    )

    result = run_sbx(["exec", "cid", "true"], max_output_bytes=1024)

    assert result.returncode == 0
    assert result.stdout == "hello"
    assert result.stderr == "oops"
    assert result.truncated is False


def test_run_sbx_capped_kills_process_on_unbounded_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `yes` writes forever; the cap must stop us long before the host buffers it all.
    monkeypatch.setattr(
        "subagents_docker_sbx._sbx.subprocess.Popen",
        _popen_shim(["sh", "-c", "yes aaaaaaaaaaaaaaaa"]),
    )

    result = run_sbx(["exec", "cid", "true"], timeout=30, max_output_bytes=4096)

    assert result.truncated is True
    assert len(result.stdout.encode()) == 4096
    assert result.returncode != 0


def test_run_sbx_capped_forwards_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "subagents_docker_sbx._sbx.subprocess.Popen",
        _popen_shim(["sh", "-c", "cat"]),
    )

    result = run_sbx(["exec", "-i", "cid", "cat"], input_text="ping", max_output_bytes=1024)

    assert result.stdout == "ping"


def test_run_sbx_capped_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "subagents_docker_sbx._sbx.subprocess.Popen",
        _popen_shim(["sh", "-c", "sleep 30"]),
    )

    with pytest.raises(SBXError, match="timed out after 1 seconds"):
        run_sbx(["exec", "cid", "sleep"], timeout=1, max_output_bytes=1024)


def _popen_shim(replacement: list[str]):
    """Replace the `sbx ...` argv with a real local command, keeping Popen behavior."""
    real_popen = subprocess.Popen

    def _factory(_args: list[str], **kwargs: object) -> subprocess.Popen:
        return real_popen(replacement, **kwargs)

    return _factory
