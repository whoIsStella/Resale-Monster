from __future__ import annotations

import asyncio
import codecs
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from goliath.core.schemas import ExecutionResult


@dataclass(slots=True)
class CapturedStream:
    value: str
    total_chars: int
    truncated: bool


class ProcessSupervisor:
    """Own subprocess groups so timeout and cancellation cannot orphan children on Linux."""

    def __init__(self, *, cancellation_grace_seconds: float = 5.0) -> None:
        self._grace_seconds = cancellation_grace_seconds
        self._active: dict[UUID, asyncio.subprocess.Process] = {}
        self._cancelled: set[UUID] = set()
        self._lock = asyncio.Lock()

    async def run(
        self,
        *,
        job_id: UUID,
        agent: str,
        command: list[str],
        redacted_command: list[str],
        workspace: Path,
        environment: dict[str, str],
        timeout_seconds: int,
        max_output_chars: int,
        on_started: Callable[[int], None] | None = None,
    ) -> ExecutionResult:
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workspace,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except (FileNotFoundError, PermissionError) as error:
            return ExecutionResult(
                job_id=job_id,
                agent=agent,
                command=redacted_command,
                duration_seconds=time.monotonic() - started,
                failure_reason=str(error),
            )

        async with self._lock:
            self._active[job_id] = process
        stdout_task = asyncio.create_task(self._collect(process.stdout, max_output_chars))
        stderr_task = asyncio.create_task(self._collect(process.stderr, max_output_chars))
        try:
            if on_started is not None:
                on_started(process.pid)
        except BaseException:
            await self._terminate(process)
            await asyncio.gather(stdout_task, stderr_task)
            async with self._lock:
                self._active.pop(job_id, None)
            raise
        timed_out = False
        try:
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout=timeout_seconds)
            except TimeoutError:
                timed_out = True
                await self._terminate(process)
        except asyncio.CancelledError:
            self._cancelled.add(job_id)
            await self._terminate(process)
            raise
        finally:
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            async with self._lock:
                self._active.pop(job_id, None)

        cancelled = job_id in self._cancelled
        self._cancelled.discard(job_id)
        return ExecutionResult(
            job_id=job_id,
            agent=agent,
            process_id=process.pid,
            command=redacted_command,
            exit_code=process.returncode,
            stdout=stdout.value,
            stderr=stderr.value,
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            cancelled=cancelled,
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            stdout_total_chars=stdout.total_chars,
            stderr_total_chars=stderr.total_chars,
        )

    async def cancel(self, job_id: UUID) -> bool:
        async with self._lock:
            process = self._active.get(job_id)
            if process is None:
                return False
            self._cancelled.add(job_id)
        await self._terminate(process)
        return True

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        self._signal_process_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self._grace_seconds)
        except TimeoutError:
            self._signal_process_group(process, signal.SIGKILL)
            await process.wait()

    @staticmethod
    def _signal_process_group(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    async def _collect(
        stream: asyncio.StreamReader | None, max_output_chars: int
    ) -> CapturedStream:
        if stream is None:
            return CapturedStream("", 0, False)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        parts: list[str] = []
        stored_chars = 0
        total_chars = 0
        while chunk := await stream.read(65_536):
            text = decoder.decode(chunk)
            total_chars += len(text)
            remaining = max_output_chars - stored_chars
            if remaining > 0:
                retained = text[:remaining]
                parts.append(retained)
                stored_chars += len(retained)
        tail = decoder.decode(b"", final=True)
        total_chars += len(tail)
        if stored_chars < max_output_chars:
            parts.append(tail[: max_output_chars - stored_chars])
        return CapturedStream("".join(parts), total_chars, total_chars > max_output_chars)


async def terminate_external_process_group(process_id: int, grace_seconds: float) -> bool:
    """Terminate a persisted Linux process group from a different orchestrator process."""
    if os.name != "posix" or process_id <= 1:
        return False
    try:
        if os.getpgid(process_id) != process_id:
            return False
        os.killpg(process_id, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(min(0.05, grace_seconds))
        try:
            os.killpg(process_id, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
    try:
        os.killpg(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        return False
    return True
