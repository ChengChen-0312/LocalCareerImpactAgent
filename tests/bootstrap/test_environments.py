"""Bootstrap contract for the isolated Phase 0 Python environments."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
RED_MARKER = "PHASE0_RED_T01_BOOTSTRAP_ABSENT"
ENVIRONMENT_DEPENDENCIES = {
    "control": {
        "localcareerimpact",
        "pytest==9.1.1",
        "hypothesis==6.167.1",
        "psutil==7.2.2",
        "pillow==11.3.0",
        "pymupdf==1.28.2",
        "huggingface-hub==0.36.2",
    },
    "qwen": {
        "localcareerimpact",
        "mlx==0.30.0",
        "mlx-lm==0.28.3",
        "mlx-vlm==0.3.7",
        "transformers==4.57.6",
        "torch==2.8.0",
        "torchvision==0.23.0",
        "huggingface-hub==0.36.2",
        "pillow==11.3.0",
        "numpy==2.2.6",
    },
    "embedding": {
        "localcareerimpact",
        "torch==2.3.1",
        "transformers==4.57.6",
        "safetensors==0.8.0",
        "sentencepiece==0.2.2",
        "huggingface-hub==0.36.2",
        "numpy==1.26.4",
    },
    "asr": {
        "localcareerimpact",
        "mlx==0.30.0",
        "mlx-whisper==0.4.3",
        "huggingface-hub==0.36.2",
    },
}


def require_file(path: Path) -> None:
    assert path.is_file(), f"{RED_MARKER}: required file is absent: {path.relative_to(ROOT)}"


def load_project(project: str) -> dict:
    manifest = ROOT / "environments" / project / "pyproject.toml"
    require_file(manifest)
    return tomllib.loads(manifest.read_text(encoding="utf-8"))


def test_root_package_manifest_exists() -> None:
    require_file(ROOT / "pyproject.toml")
    require_file(ROOT / "src" / "localcareerimpact" / "__init__.py")


@pytest.mark.parametrize("project", ENVIRONMENT_DEPENDENCIES)
def test_environment_manifest_has_only_pinned_required_dependencies(project: str) -> None:
    version_file = ROOT / "environments" / project / ".python-version"
    require_file(version_file)
    assert version_file.read_text(encoding="utf-8") == "3.12.12\n"

    manifest = load_project(project)
    assert manifest["project"]["requires-python"] == "==3.12.12"
    dependencies = {dependency.lower() for dependency in manifest["project"]["dependencies"]}
    assert dependencies == ENVIRONMENT_DEPENDENCIES[project]
    assert "mlx-embeddings" not in dependencies
    assert all(
        dependency == "localcareerimpact" or "==" in dependency
        for dependency in dependencies
    )
    assert manifest["tool"]["uv"]["sources"] == {
        "localcareerimpact": {"path": "../..", "editable": True}
    }


def test_qwen_manifest_keeps_required_cpu_processor_pair() -> None:
    dependencies = {
        dependency.lower()
        for dependency in load_project("qwen")["project"]["dependencies"]
    }
    assert {"torch==2.8.0", "torchvision==0.23.0"} <= dependencies


@pytest.mark.parametrize("project", ENVIRONMENT_DEPENDENCIES)
def test_environment_interpreter_is_target_arm64_cpython(project: str) -> None:
    interpreter = ROOT / "environments" / project / ".venv" / "bin" / "python"
    require_file(interpreter)
    command = [
        str(interpreter),
        "-c",
        "import platform, sys; print(platform.machine()); print(sys.version.split()[0])",
    ]
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    assert result.stdout.splitlines() == ["arm64", "3.12.12"]


def test_qwen_environment_exposes_frozen_processor_records() -> None:
    interpreter = ROOT / "environments" / "qwen" / ".venv" / "bin" / "python"
    require_file(interpreter)
    result = subprocess.run(
        [
            str(interpreter),
            "-c",
            "from transformers import Qwen2VLImageProcessorFast, Qwen3VLVideoProcessor",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
