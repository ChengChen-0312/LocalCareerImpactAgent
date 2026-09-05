"""Application-owned model lifetime and shared Metal execution gate."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from localcareerimpact.workers.client import ResidentWorkerClient, WorkerStatus
from localcareerimpact.workers.protocol import GenerateRequest, ModelKey, ResultEnvelope

from .config import AppSettings


class ModelRuntime:
    def __init__(self, settings: AppSettings) -> None:
        self.metal_semaphore = asyncio.Semaphore(1)
        self._workers = ResidentWorkerClient(
            repository_root=settings.repository_root,
            qwen_python_path=settings.qwen_python_path,
            models_config_path=settings.models_config_path,
        )

    @property
    def statuses(self) -> dict[ModelKey, WorkerStatus]:
        return self._workers.statuses

    async def start(self) -> None:
        await self._workers.start()

    async def stop(self) -> None:
        await self._workers.stop()

    @asynccontextmanager
    async def metal_operation(self) -> AsyncIterator[None]:
        """Gate Qwen and every future embedding, ASR, or accelerated OCR call."""

        async with self.metal_semaphore:
            yield

    async def generate(self, model_key: ModelKey, request: GenerateRequest) -> ResultEnvelope:
        async with self.metal_operation():
            return await self._workers.generate(model_key, request)

    async def restart(self, model_key: ModelKey) -> None:
        async with self.metal_operation():
            await self._workers.restart(model_key)

    async def restart_if_failed(self, model_key: ModelKey) -> bool:
        """Atomically restart exactly one failed worker under the shared Metal gate."""

        async with self.metal_operation():
            if self._workers.statuses[model_key] != "failed":
                return False
            await self._workers.restart(model_key)
            return True
