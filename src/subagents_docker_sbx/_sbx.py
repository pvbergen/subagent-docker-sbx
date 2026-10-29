import contextlib
import json
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import IO

from .errors import SBXError

SBX_BINARY = "sbx"
_READ_CHUNK_BYTES = 65_536
_POLL_INTERVAL = 0.05


@dataclass(frozen=True)
class SBXRunResult:
    """Captured output from an sbx CLI subprocess."""

    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False


def run_sbx(
    args: Sequence[str],
    *,
    timeout: float | None = None,
    input_text: str | None = None,
    max_output_bytes: int | None = None,
) -> SBXRunResult:
    """Run `sbx` with the given arguments.

    When ``max_output_bytes`` is set, stdout and stderr are streamed and each is capped at
    that many bytes, killing the subprocess once the cap is hit. This keeps a command that
    writes unbounded output (for example ``dd if=/dev/zero``) from exhausting host memory.
    """
    if max_output_bytes is not None:
        return _run_sbx_capped(
            args,
            timeout=timeout,
            input_text=input_text,
            max_output_bytes=max_output_bytes,
        )

    try:
        completed = subprocess.run(  # noqa: S603
            [SBX_BINARY, *args],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"sbx command timed out after {timeout} seconds"
        raise SBXError(msg) from exc
    except FileNotFoundError as exc:
        msg = "sbx executable not found on PATH"
        raise SBXError(msg) from exc

    return SBXRunResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def _drain(stream: IO[bytes], chunks: list[bytes], limit: int, hit_cap: threading.Event) -> None:
    """Read `stream` into `chunks`, stopping once `limit` bytes have been collected."""
    total = 0
    try:
        while True:
            chunk = stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            chunks.append(chunk)
            total += len(chunk)
            if total >= limit:
                hit_cap.set()
                return
    except (OSError, ValueError):
        return
    finally:
        with contextlib.suppress(Exception):
            stream.close()


def _write_stdin(stream: IO[bytes], payload: bytes) -> None:
    try:
        stream.write(payload)
    except (OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(Exception):
            stream.close()


def _run_sbx_capped(
    args: Sequence[str],
    *,
    timeout: float | None,
    input_text: str | None,
    max_output_bytes: int,
) -> SBXRunResult:
    try:
        process = subprocess.Popen(  # noqa: S603
            [SBX_BINARY, *args],  # noqa: S607
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        msg = "sbx executable not found on PATH"
        raise SBXError(msg) from exc

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    hit_cap = threading.Event()
    workers = [
        threading.Thread(
            target=_drain,
            args=(process.stdout, stdout_chunks, max_output_bytes, hit_cap),
            daemon=True,
        ),
        threading.Thread(
            target=_drain,
            args=(process.stderr, stderr_chunks, max_output_bytes, hit_cap),
            daemon=True,
        ),
    ]
    if input_text is not None and process.stdin is not None:
        workers.append(
            threading.Thread(
                target=_write_stdin,
                args=(process.stdin, input_text.encode()),
                daemon=True,
            ),
        )
    for worker in workers:
        worker.start()

    deadline = None if timeout is None else time.monotonic() + timeout
    timed_out = False
    try:
        while any(worker.is_alive() for worker in workers):
            if hit_cap.is_set():
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(_POLL_INTERVAL)
    finally:
        if hit_cap.is_set() or timed_out:
            _terminate(process)
        try:
            returncode = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _terminate(process)
            returncode = process.wait()
        for worker in workers:
            worker.join(timeout=1)

    if timed_out:
        msg = f"sbx command timed out after {timeout} seconds"
        raise SBXError(msg)

    stdout = _decode(stdout_chunks, max_output_bytes)
    stderr = _decode(stderr_chunks, max_output_bytes)
    return SBXRunResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        truncated=hit_cap.is_set(),
    )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(Exception):
        process.kill()


def _decode(chunks: list[bytes], limit: int) -> str:
    return b"".join(chunks)[:limit].decode("utf-8", errors="replace")


def sbx_unavailable_reason(*, timeout: float | None = None) -> str | None:
    """Return why sbx cannot create sandboxes, or ``None`` when it is ready.

    ``sbx ls`` needs both a running daemon and a signed-in Docker account, so it catches the
    "not signed in" case that a bare ``sbx version`` would miss.
    """
    try:
        result = run_sbx(["ls", "-q"], timeout=timeout)
    except SBXError as exc:
        return str(exc)
    if result.returncode != 0:
        return format_sbx_error(result)
    return None


def sbx_available(*, timeout: float | None = None) -> bool:
    """Return True when the sbx daemon responds to `sbx ls` within `timeout`."""
    return sbx_unavailable_reason(timeout=timeout) is None


def format_sbx_error(result: SBXRunResult) -> str:
    """Combine stderr/stdout into a single error string."""
    detail = (result.stderr or result.stdout).strip()
    if not detail:
        detail = f"exit code {result.returncode}"
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return detail
    if isinstance(payload, dict) and "message" in payload:
        return str(payload["message"])
    return detail
