"""Hatchling build hook for a macOS-safe editable MVP install."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class EditablePackageBuildHook(BuildHookInterface):
    """Embed the real package only in editable wheels.

    macOS can hide Hatchling's underscore-prefixed editable ``.pth`` file,
    which CPython then ignores. Embedding the package keeps the install usable
    without changing the normal wheel or relying on that path entry.
    """

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version != "editable":
            return
        build_data["force_include_editable"] = {
            str(Path(self.root) / "src" / "localcareerimpact"): "localcareerimpact"
        }
