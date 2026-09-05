"""CSV-backed demo authentication and process-local sessions."""

from __future__ import annotations

import csv
import secrets
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigurationError, EXPECTED_USERS_HEADER


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    username: str


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionPrincipal] = {}

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = SessionPrincipal(username=username)
        return token

    def resolve(self, token: str | None) -> SessionPrincipal | None:
        return None if token is None else self._sessions.get(token)

    def revoke(self, token: str | None) -> None:
        if token is not None:
            self._sessions.pop(token, None)


class CsvCredentialStore:
    """Reload local credentials for every authentication attempt."""

    def __init__(self, users_csv_path: Path) -> None:
        self._users_csv_path = users_csv_path

    def validate_configuration(self) -> None:
        self._read_rows()

    def authenticate(self, username: str, password: str) -> SessionPrincipal | None:
        for row in self._read_rows():
            if (
                row["username"] == username
                and row["password"] == password
                and row["enabled"] == "true"
            ):
                return SessionPrincipal(username=username)
        return None

    def _read_rows(self) -> list[dict[str, str]]:
        try:
            with self._users_csv_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != EXPECTED_USERS_HEADER:
                    raise ConfigurationError(
                        "config/users.csv must have the exact header: username,password,enabled"
                    )
                return list(reader)
        except OSError as exc:
            raise ConfigurationError(f"Unable to read config/users.csv: {exc}") from exc
        except csv.Error as exc:
            raise ConfigurationError(f"Unable to parse config/users.csv: {exc}") from exc
