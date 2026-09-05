"""Configuration resolved from the repository root."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from urllib.parse import unquote, urlparse


EXPECTED_USERS_HEADER = ["username", "password", "enabled"]
EXPECTED_MODEL_KEYS = frozenset({"qwen30b", "qwen4b", "embedding", "whisper"})
REPOSITORY_ROOT_ENV = "LOCALCAREERIMPACT_REPOSITORY_ROOT"


class ConfigurationError(RuntimeError):
    """Raised when required local operator configuration is unusable."""


@dataclass(frozen=True, slots=True)
class ModelPaths:
    qwen30b: Path
    qwen4b: Path
    embedding: Path
    whisper: Path

    @property
    def local_files_only(self) -> bool:
        return True

    @classmethod
    def from_file(cls, config_path: Path, *, validate_all_paths: bool = True) -> ModelPaths:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(
                "Missing config/models.local.json. Copy config/models.local.json.example "
                "and configure local model directories."
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError("config/models.local.json must be readable JSON.") from exc

        if not isinstance(raw, dict) or set(raw) != EXPECTED_MODEL_KEYS:
            raise ConfigurationError(
                "config/models.local.json must contain exactly qwen30b, qwen4b, "
                "embedding, and whisper."
            )
        if any(
            not isinstance(raw[key], str) or not raw[key].strip()
            for key in EXPECTED_MODEL_KEYS
        ):
            raise ConfigurationError("Every configured model path must be a non-empty string.")

        paths = cls(**{key: Path(raw[key]).expanduser().resolve() for key in EXPECTED_MODEL_KEYS})
        if validate_all_paths:
            paths.validate()
        return paths

    def validate(self) -> None:
        for key in sorted(EXPECTED_MODEL_KEYS):
            if not getattr(self, key).is_dir():
                raise ConfigurationError(f"Configured model directory for {key} does not exist.")

    def qwen_path(self, model_key: str) -> Path:
        if model_key not in {"qwen30b", "qwen4b"}:
            raise ConfigurationError("Qwen model key must be qwen30b or qwen4b.")
        path = getattr(self, model_key)
        if not path.is_dir():
            raise ConfigurationError(f"Configured model directory for {model_key} does not exist.")
        return path


@dataclass(frozen=True, slots=True)
class AppSettings:
    repository_root: Path
    database_path: Path
    users_csv_path: Path
    upload_root: Path
    knowledge_inbox: Path
    models_config_path: Path
    qwen_python_path: Path
    admin_username: str

    @classmethod
    def from_repository_root(cls, repository_root: Path | None = None) -> AppSettings:
        root = (
            repository_root.resolve()
            if repository_root is not None
            else cls._find_repository_root()
        )
        return cls(
            repository_root=root,
            database_path=root / "var" / "localcareerimpact.sqlite3",
            users_csv_path=root / "config" / "users.csv",
            upload_root=root / "var" / "tmp" / "uploads",
            knowledge_inbox=root / "data" / "knowledge" / "inbox",
            models_config_path=root / "config" / "models.local.json",
            qwen_python_path=root / "environments" / "qwen" / ".venv" / "bin" / "python",
            admin_username="admin",
        )

    @staticmethod
    def _find_repository_root() -> Path:
        configured_root = os.environ.get(REPOSITORY_ROOT_ENV)
        if configured_root:
            candidate = Path(configured_root).expanduser().resolve()
            if AppSettings._is_repository_root(candidate):
                return candidate
            raise ConfigurationError(
                f"{REPOSITORY_ROOT_ENV} must point to a LocalCareerImpactAgent repository root."
            )

        for candidate in AppSettings._repository_root_candidates():
            if AppSettings._is_repository_root(candidate):
                return candidate
        raise ConfigurationError(
            "Could not find the LocalCareerImpactAgent repository root. "
            f"Set {REPOSITORY_ROOT_ENV} when starting outside the checkout."
        )

    @staticmethod
    def _repository_root_candidates() -> tuple[Path, ...]:
        module_path = Path(__file__).resolve()
        working_directory = Path.cwd().resolve()
        candidates = [*module_path.parents, *working_directory.parents, working_directory]
        source_root = AppSettings._direct_url_root()
        if source_root is not None:
            candidates.append(source_root)
        return tuple(dict.fromkeys(candidates))

    @staticmethod
    def _direct_url_root() -> Path | None:
        try:
            direct_url = distribution("localcareerimpact").read_text("direct_url.json")
        except PackageNotFoundError:
            return None
        if direct_url is None:
            return None
        try:
            source_url = json.loads(direct_url)["url"]
            parsed_url = urlparse(source_url)
        except (KeyError, TypeError, ValueError):
            return None
        if parsed_url.scheme != "file":
            return None
        return Path(unquote(parsed_url.path)).resolve()

    @staticmethod
    def _is_repository_root(candidate: Path) -> bool:
        return (candidate / "pyproject.toml").is_file() and (
            candidate / "src" / "localcareerimpact"
        ).is_dir()

    def validate_users_csv(self) -> None:
        if not self.users_csv_path.is_file():
            raise ConfigurationError(
                "Missing config/users.csv. Copy config/users.csv.example and configure an enabled local account."
            )

        try:
            with self.users_csv_path.open("r", encoding="utf-8", newline="") as handle:
                header = next(csv.reader(handle), None)
        except OSError as exc:
            raise ConfigurationError(f"Unable to read config/users.csv: {exc}") from exc

        if header != EXPECTED_USERS_HEADER:
            raise ConfigurationError(
                "config/users.csv must have the exact header: username,password,enabled"
            )

    def load_model_paths(self) -> ModelPaths:
        if not self.qwen_python_path.is_file():
            raise ConfigurationError("The pinned Qwen Python environment is unavailable.")
        return ModelPaths.from_file(self.models_config_path)
