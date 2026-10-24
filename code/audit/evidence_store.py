"""Raw access to the evidence files listed in evidence_files.csv.

Files are loaded lazily and cached: text files are decoded once, JSON is parsed
once, and each SQL snapshot is executed once into its own in-memory SQLite
database. Interpreting the contents (sightings) is Phase 3's job.

SQL exports are untrusted input. They run only in a private ":memory:"
database, under an authorizer that refuses ATTACH/DETACH (the only statements
that could touch the filesystem) and every PRAGMA except foreign_keys. Once
loaded, the connection is switched to query_only with a read-only authorizer.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from audit.loader import Dataset, DatasetError
from audit.models import EvidenceFile

TEXT_KINDS = {"sql", "json", "log", "txt"}
BINARY_KINDS = {"pdf", "png"}

_DENY_WHILE_LOADING = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
_ALLOW_WHEN_READING = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
# The exports begin with "PRAGMA foreign_keys=ON"; every other PRAGMA is refused.
_SAFE_LOADING_PRAGMAS = {"foreign_keys"}


def _loading_authorizer(action, arg1, arg2, db_name, trigger):
    if action == sqlite3.SQLITE_PRAGMA:
        return sqlite3.SQLITE_OK if arg1 in _SAFE_LOADING_PRAGMAS else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY if action in _DENY_WHILE_LOADING else sqlite3.SQLITE_OK


def _reading_authorizer(action, arg1, arg2, db_name, trigger):
    if action in _ALLOW_WHEN_READING:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 == "table_info":
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def open_sql_snapshot(script: str, where: str = "<sql>") -> sqlite3.Connection:
    """Execute an untrusted SQL export into an isolated, then read-only, database."""
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA query_only = OFF")
    connection.set_authorizer(_loading_authorizer)
    try:
        connection.executescript(script)
    except sqlite3.Error as exc:
        connection.close()
        raise DatasetError(f"{where}: SQL snapshot failed to load ({exc})") from None
    connection.set_authorizer(None)
    connection.execute("PRAGMA query_only = ON")
    connection.set_authorizer(_reading_authorizer)
    connection.row_factory = sqlite3.Row
    return connection


class EvidenceStore:
    def __init__(self, dataset: Dataset):
        self.dataset = dataset
        self._text: dict[str, str] = {}
        self._json: dict[str, Any] = {}
        self._sql: dict[str, sqlite3.Connection] = {}

    def file(self, file_id: str) -> EvidenceFile:
        try:
            return self.dataset.evidence_files[file_id]
        except KeyError:
            raise DatasetError(f"unknown evidence file {file_id!r}") from None

    def path(self, file_id: str) -> Path:
        return self.dataset.root / self.file(file_id).file_path

    def text(self, file_id: str) -> str:
        if file_id not in self._text:
            kind = self.file(file_id).kind
            if kind not in TEXT_KINDS:
                raise DatasetError(f"{file_id} is a {kind} file, not text")
            self._text[file_id] = self.path(file_id).read_text(encoding="utf-8")
        return self._text[file_id]

    def lines(self, file_id: str) -> list[str]:
        return self.text(file_id).splitlines()

    def json(self, file_id: str) -> Any:
        if file_id not in self._json:
            if self.file(file_id).kind != "json":
                raise DatasetError(f"{file_id} is not a JSON file")
            try:
                self._json[file_id] = json.loads(self.text(file_id))
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{file_id}: invalid JSON ({exc.msg} at line {exc.lineno})") from None
        return self._json[file_id]

    def sql(self, file_id: str) -> sqlite3.Connection:
        if file_id not in self._sql:
            if self.file(file_id).kind != "sql":
                raise DatasetError(f"{file_id} is not a SQL snapshot")
            self._sql[file_id] = open_sql_snapshot(self.text(file_id), file_id)
        return self._sql[file_id]

    def sql_tables(self, file_id: str) -> dict[str, list[dict]]:
        """Every table of a snapshot as a list of row dicts, in rowid order."""
        connection = self.sql(file_id)
        names = [r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {name: [dict(row) for row in connection.execute(f'SELECT * FROM "{name}"')]
                for name in names}

    def captured_by(self, moment: datetime) -> list[EvidenceFile]:
        """Files whose manifest captured_at is at or before `moment`, oldest first.

        These are the files a step may cite for a case audited at `moment`.
        """
        files = [f for f in self.dataset.evidence_files.values() if f.captured_at <= moment]
        return sorted(files, key=lambda f: (f.captured_at, f.file_id))

    def close(self) -> None:
        for connection in self._sql.values():
            connection.close()
        self._sql.clear()
