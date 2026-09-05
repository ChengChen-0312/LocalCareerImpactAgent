"""Async owner for the two private resident Qwen subprocesses."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from .protocol import (
    ErrorEnvelope,
    GenerateRequest,
    GenerationFailureCategory,
    GenerationMetrics,
    ModelKey,
    ReadyEnvelope,
    ResultEnvelope,
    parse_worker_envelope,
    sanitize_schema_error_path,
)


LOGGER = logging.getLogger(__name__)
WorkerStatus = Literal["starting", "ready", "failed"]
_PUBLIC_WORKER_FAILURE_MESSAGE = "local worker request failed"


class WorkerStartupError(RuntimeError):
    def __init__(self, model_key: ModelKey, reason: str) -> None:
        self.model_key = model_key
        self.reason = reason
        super().__init__(f"{model_key} worker readiness failed: {reason}")


class WorkerCallError(RuntimeError):
    __slots__ = (
        "_code",
        "_category",
        "_metrics",
        "_parse_error_offset",
        "_schema_error_path",
    )

    def __init__(
        self,
        message: str,
        *,
        code: Literal[
            "invalid_request", "generation_failed", "startup_failed"
        ]
        | None = None,
        category: GenerationFailureCategory | None = None,
        metrics: GenerationMetrics | None = None,
        parse_error_offset: int | None = None,
        schema_error_path: tuple[int | str, ...] = (),
    ) -> None:
        self._code = code
        self._category = category
        self._metrics = metrics
        self._parse_error_offset = parse_error_offset
        self._schema_error_path = schema_error_path
        super().__init__(message)

    @property
    def code(
        self,
    ) -> Literal["invalid_request", "generation_failed", "startup_failed"] | None:
        return self._code

    @property
    def category(self) -> GenerationFailureCategory | None:
        return self._category

    @property
    def metrics(self) -> GenerationMetrics | None:
        return self._metrics

    @property
    def parse_error_offset(self) -> int | None:
        return self._parse_error_offset

    @property
    def schema_error_path(self) -> tuple[int | str, ...]:
        return self._schema_error_path


@dataclass(slots=True)
class _WorkerProcess:
    model_key: ModelKey
    status: WorkerStatus = "starting"
    process: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[None] | None = None
    io_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ResidentWorkerClient:
    """Starts, calls, and stops the exact configured 30B and 4B workers."""

    def __init__(
        self,
        *,
        repository_root: Path,
        qwen_python_path: Path,
        models_config_path: Path,
        ready_timeout_seconds: float = 300.0,
        generation_timeout_seconds: float = 600.0,
    ) -> None:
        self._repository_root = repository_root
        self._qwen_python_path = qwen_python_path
        self._models_config_path = models_config_path
        self._ready_timeout_seconds = ready_timeout_seconds
        self._generation_timeout_seconds = generation_timeout_seconds
        self._workers: dict[ModelKey, _WorkerProcess] = {
            "qwen30b": _WorkerProcess("qwen30b"),
            "qwen4b": _WorkerProcess("qwen4b"),
        }

    @property
    def statuses(self) -> dict[ModelKey, WorkerStatus]:
        for worker in self._workers.values():
            if (
                worker.status != "failed"
                and worker.process is not None
                and worker.process.returncode is not None
            ):
                worker.status = "failed"
        return {key: worker.status for key, worker in self._workers.items()}

    async def start(self) -> None:
        tasks = [
            asyncio.create_task(self._start_worker(worker))
            for worker in self._workers.values()
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.stop()
            raise

    async def _start_worker(self, worker: _WorkerProcess) -> None:
        script_path = (
            self._repository_root
            / "src"
            / "localcareerimpact"
            / "workers"
            / "qwen_worker.py"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        worker.status = "starting"
        try:
            worker.process = await asyncio.create_subprocess_exec(
                str(self._qwen_python_path),
                str(script_path),
                "--model-key",
                worker.model_key,
                "--config",
                str(self._models_config_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._repository_root,
                env=environment,
            )
            worker.stderr_task = asyncio.create_task(self._drain_stderr(worker))
            envelope = await self._read_envelope(worker, self._ready_timeout_seconds)
            if isinstance(envelope, ErrorEnvelope):
                raise WorkerStartupError(worker.model_key, envelope.message)
            if not isinstance(envelope, ReadyEnvelope) or envelope.model_key != worker.model_key:
                raise WorkerStartupError(worker.model_key, "invalid readiness response")
            worker.status = "ready"
        except asyncio.TimeoutError as exc:
            worker.status = "failed"
            raise WorkerStartupError(worker.model_key, "readiness timed out") from exc
        except WorkerStartupError:
            worker.status = "failed"
            raise
        except Exception as exc:
            worker.status = "failed"
            raise WorkerStartupError(worker.model_key, "worker process could not start") from exc

    async def _drain_stderr(self, worker: _WorkerProcess) -> None:
        process = worker.process
        if process is None or process.stderr is None:
            return
        while line := await process.stderr.readline():
            LOGGER.debug("%s worker: %s", worker.model_key, line.decode(errors="replace").rstrip())

    async def _read_envelope(
        self,
        worker: _WorkerProcess,
        timeout_seconds: float,
    ) -> ReadyEnvelope | ResultEnvelope | ErrorEnvelope:
        process = worker.process
        if process is None or process.stdout is None:
            raise WorkerCallError(f"{worker.model_key} worker is not running")
        line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout_seconds)
        if not line:
            worker.status = "failed"
            raise WorkerCallError(f"{worker.model_key} worker exited without a protocol response")
        try:
            return parse_worker_envelope(line)
        except ValidationError as exc:
            worker.status = "failed"
            raise WorkerCallError(
                f"{worker.model_key} worker returned an invalid protocol response"
            ) from exc

    async def generate(self, model_key: ModelKey, request: GenerateRequest) -> ResultEnvelope:
        worker = self._workers[model_key]
        async with worker.io_lock:
            process = worker.process
            if worker.status != "ready" or process is None or process.stdin is None:
                raise WorkerCallError(f"{model_key} worker is not ready")
            try:
                process.stdin.write(request.model_dump_json().encode("utf-8") + b"\n")
                await process.stdin.drain()
                envelope = await self._read_envelope(worker, self._generation_timeout_seconds)
            except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
                worker.status = "failed"
                raise WorkerCallError(f"{model_key} worker stopped during generation") from exc
            except asyncio.TimeoutError as exc:
                await self._cleanup_worker(worker, mark_failed=True)
                raise WorkerCallError(f"{model_key} worker generation timed out") from exc
            except asyncio.CancelledError:
                await self._cleanup_worker(worker, mark_failed=True)
                raise

            if isinstance(envelope, ErrorEnvelope):
                if envelope.request_id != request.request_id:
                    worker.status = "failed"
                    raise WorkerCallError(
                        f"{model_key} worker returned a mismatched error response"
                    )
                raise WorkerCallError(
                    _PUBLIC_WORKER_FAILURE_MESSAGE,
                    code=envelope.code,
                    category=envelope.category,
                    metrics=envelope.metrics,
                    parse_error_offset=envelope.parse_error_offset,
                    schema_error_path=sanitize_schema_error_path(
                        envelope.schema_error_path,
                        request.response_schema,
                    ),
                )
            if (
                not isinstance(envelope, ResultEnvelope)
                or envelope.request_id != request.request_id
            ):
                worker.status = "failed"
                raise WorkerCallError(f"{model_key} worker returned a mismatched response")
            return envelope

    async def restart(self, model_key: ModelKey) -> None:
        """Explicitly replace one failed worker after its old process is fully gone."""

        worker = self._workers[model_key]
        async with worker.io_lock:
            await self._cleanup_worker(worker, mark_failed=True)
            try:
                await self._start_worker(worker)
            except BaseException:
                await self._cleanup_worker(worker, mark_failed=True)
                raise

    async def stop(self) -> None:
        results = await asyncio.gather(
            *(self._cleanup_worker(worker) for worker in self._workers.values()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                LOGGER.warning("A worker cleanup operation failed (%s).", type(result).__name__)

    async def _cleanup_worker(
        self,
        worker: _WorkerProcess,
        *,
        mark_failed: bool = False,
    ) -> None:
        if mark_failed:
            worker.status = "failed"
        process = worker.process
        stderr_task = worker.stderr_task

        try:
            if process is not None and process.returncode is None:
                try:
                    process.terminate()
                except Exception as exc:
                    self._log_cleanup_error(worker, exc)

            if process is not None and process.stdin is not None:
                try:
                    process.stdin.close()
                    await asyncio.wait_for(process.stdin.wait_closed(), timeout=1.0)
                except Exception as exc:
                    self._log_cleanup_error(worker, exc)

            if process is not None and process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except Exception as exc:
                        self._log_cleanup_error(worker, exc)
                    try:
                        await process.wait()
                    except Exception as exc:
                        self._log_cleanup_error(worker, exc)
                except Exception as exc:
                    self._log_cleanup_error(worker, exc)

            if process is not None and process.stdout is not None:
                try:
                    await asyncio.wait_for(process.stdout.read(), timeout=1.0)
                except Exception as exc:
                    self._log_cleanup_error(worker, exc)
        finally:
            try:
                if stderr_task is not None:
                    if not stderr_task.done():
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(stderr_task),
                                timeout=1.0,
                            )
                        except asyncio.TimeoutError:
                            stderr_task.cancel()
                        except Exception as exc:
                            self._log_cleanup_error(worker, exc)
                    await asyncio.gather(stderr_task, return_exceptions=True)
            finally:
                worker.process = None
                worker.stderr_task = None

    @staticmethod
    def _log_cleanup_error(worker: _WorkerProcess, error: Exception) -> None:
        LOGGER.warning(
            "%s worker cleanup step failed (%s).",
            worker.model_key,
            type(error).__name__,
        )
