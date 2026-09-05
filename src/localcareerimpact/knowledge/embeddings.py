"""Offline embedding subprocess and little-endian float32 wire format."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import sys
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from localcareerimpact.app.model_runtime import ModelRuntime


EMBEDDING_TIMEOUT_SECONDS = 10 * 60
MAX_EMBEDDING_REQUEST_BYTES = 32 * 1024 * 1024
MAX_EMBEDDING_STDERR_BYTES = 64 * 1024


class EmbeddingError(RuntimeError):
    """Raised when the pinned local embedding runtime cannot produce vectors."""


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    blob: bytes
    dimension: int

    def values(self) -> tuple[float, ...]:
        values = array("f")
        values.frombytes(self.blob)
        if sys.byteorder != "little":
            values.byteswap()
        return tuple(values)


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: tuple[EmbeddingVector, ...]
    model_fingerprint: str


class EmbeddingClient:
    def __init__(
        self,
        *,
        python_path: Path,
        model_path: Path,
        repository_root: Path,
        runtime: ModelRuntime,
        timeout_seconds: float = EMBEDDING_TIMEOUT_SECONDS,
    ) -> None:
        self._python_path = python_path
        self._model_path = model_path
        self._repository_root = repository_root
        self._runtime = runtime
        self._timeout_seconds = timeout_seconds

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        if not texts:
            raise EmbeddingError("At least one text is required for embedding inference.")
        payload = json.dumps(
            {"texts": list(texts)}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(payload) > MAX_EMBEDDING_REQUEST_BYTES:
            raise EmbeddingError("The embedding request is too large for one local rebuild.")
        if not self._python_path.is_file():
            raise EmbeddingError("The pinned embedding Python environment is unavailable.")
        if not self._model_path.is_dir():
            raise EmbeddingError("The configured local embedding model is unavailable.")

        async with self._runtime.metal_operation():
            stdout, stderr, returncode = await self._invoke_worker(payload)
        if returncode != 0:
            reason = _sanitized_worker_error(stderr)
            raise EmbeddingError(reason)
        try:
            decoded = json.loads(stdout)
            dimension = decoded["dimension"]
            encoded_vectors = decoded["embeddings"]
            model_fingerprint = decoded["model_fingerprint"]
            if not isinstance(dimension, int) or dimension < 1:
                raise ValueError
            if (
                not isinstance(model_fingerprint, str)
                or len(model_fingerprint) != 71
                or not model_fingerprint.startswith("sha256:")
                or any(
                    character not in "0123456789abcdef"
                    for character in model_fingerprint.removeprefix("sha256:")
                )
            ):
                raise ValueError
            if not isinstance(encoded_vectors, list) or len(encoded_vectors) != len(texts):
                raise ValueError
            vectors = tuple(
                EmbeddingVector(
                    blob=base64.b64decode(item, validate=True),
                    dimension=dimension,
                )
                for item in encoded_vectors
                if isinstance(item, str)
            )
            if len(vectors) != len(texts):
                raise ValueError
            for vector in vectors:
                if len(vector.blob) != vector.dimension * 4:
                    raise ValueError
                if not all(math.isfinite(value) for value in vector.values()):
                    raise ValueError
            return EmbeddingResult(
                vectors=vectors,
                model_fingerprint=model_fingerprint,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EmbeddingError("The local embedding worker returned an invalid response.") from exc

    async def _invoke_worker(self, payload: bytes) -> tuple[bytes, bytes, int]:
        environment = os.environ.copy()
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
                "TOKENIZERS_PARALLELISM": "false",
            }
        )
        process = await asyncio.create_subprocess_exec(
            str(self._python_path),
            "-I",
            "-m",
            "localcareerimpact.knowledge.embeddings",
            "--worker",
            str(self._model_path),
            cwd=self._repository_root,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        communication = asyncio.create_task(process.communicate(payload))

        async def terminate_and_reap() -> None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.shield(communication)

        try:
            async with asyncio.timeout(self._timeout_seconds):
                stdout, stderr = await asyncio.shield(communication)
        except asyncio.TimeoutError as exc:
            await terminate_and_reap()
            raise EmbeddingError("Local embedding inference timed out.") from exc
        except asyncio.CancelledError:
            await terminate_and_reap()
            raise
        except BaseException:
            await terminate_and_reap()
            raise
        return stdout, stderr[:MAX_EMBEDDING_STDERR_BYTES], int(process.returncode or 0)


def _sanitized_worker_error(stderr: bytes) -> str:
    controlled_messages = {
        "The embedding request is invalid.",
        "The pinned embedding dependencies are unavailable.",
        "The configured local embedding model could not be loaded.",
        "The configured local embedding model changed during inference.",
        "Local embedding inference failed.",
    }
    try:
        message = stderr.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        return "The local embedding worker failed."
    return message if message in controlled_messages else "The local embedding worker failed."


def _model_directory_fingerprint(model_path: Path) -> str:
    if not model_path.is_dir():
        raise EmbeddingError("The configured local embedding model is unavailable.")
    digest = hashlib.sha256()
    files = sorted(
        (path for path in model_path.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(model_path).as_posix(),
    )
    if not files:
        raise EmbeddingError("The configured local embedding model is unavailable.")
    for path in files:
        relative = path.relative_to(model_path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        try:
            with path.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    digest.update(block)
        except OSError as exc:
            raise EmbeddingError(
                "The configured local embedding model is unavailable."
            ) from exc
    return f"sha256:{digest.hexdigest()}"


def _worker_embed(model_path: Path, texts: list[str]) -> tuple[int, list[str], str]:
    try:
        import numpy as np
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("dependencies") from exc

    try:
        fingerprint_before_load = _model_directory_fingerprint(model_path)
        tokenizer = AutoTokenizer.from_pretrained(
            model_path,
            local_files_only=True,
            trust_remote_code=False,
        )
        model = AutoModel.from_pretrained(
            model_path,
            local_files_only=True,
            trust_remote_code=False,
        )
        fingerprint_after_load = _model_directory_fingerprint(model_path)
    except Exception as exc:
        raise RuntimeError("model") from exc
    if fingerprint_before_load != fingerprint_after_load:
        raise RuntimeError("model_changed")

    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    model.to(device)
    model.eval()
    encoded_vectors: list[str] = []
    dimension = 0
    try:
        with torch.inference_mode():
            for offset in range(0, len(texts), 32):
                batch = texts[offset : offset + 32]
                encoded = tokenizer(
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=256,
                    return_tensors="pt",
                )
                encoded = {key: tensor.to(device) for key, tensor in encoded.items()}
                output = model(**encoded).last_hidden_state
                mask = encoded["attention_mask"].unsqueeze(-1).expand(output.size()).float()
                pooled = torch.sum(output * mask, dim=1) / torch.clamp(
                    mask.sum(dim=1), min=1e-9
                )
                pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
                values = pooled.detach().to("cpu").float().numpy()
                if values.ndim != 2 or values.shape[0] != len(batch):
                    raise ValueError("unexpected embedding shape")
                dimension = int(values.shape[1])
                for vector in values:
                    little_endian = np.asarray(vector, dtype="<f4")
                    encoded_vectors.append(
                        base64.b64encode(little_endian.tobytes(order="C")).decode("ascii")
                    )
    except Exception as exc:
        raise RuntimeError("inference") from exc
    return dimension, encoded_vectors, fingerprint_before_load


def _worker_main(model_path: Path) -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_EMBEDDING_REQUEST_BYTES + 1)
        if len(raw) > MAX_EMBEDDING_REQUEST_BYTES:
            raise ValueError
        request = json.loads(raw)
        texts = request["texts"]
        if (
            not isinstance(texts, list)
            or not texts
            or any(not isinstance(text, str) or not text.strip() for text in texts)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        sys.stderr.write("The embedding request is invalid.")
        return 2
    try:
        dimension, vectors, model_fingerprint = _worker_embed(model_path, texts)
    except RuntimeError as exc:
        category = str(exc)
        if category == "dependencies":
            message = "The pinned embedding dependencies are unavailable."
        elif category == "model":
            message = "The configured local embedding model could not be loaded."
        elif category == "model_changed":
            message = "The configured local embedding model changed during inference."
        else:
            message = "Local embedding inference failed."
        sys.stderr.write(message)
        return 3
    sys.stdout.write(
        json.dumps(
            {
                "dimension": dimension,
                "embeddings": vectors,
                "model_fingerprint": model_fingerprint,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = _parse_args()
    raise SystemExit(_worker_main(arguments.worker.resolve()))


__all__ = ["EmbeddingClient", "EmbeddingError", "EmbeddingResult", "EmbeddingVector"]
