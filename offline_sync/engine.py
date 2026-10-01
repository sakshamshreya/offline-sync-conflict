"""Versioned document sync and field-level conflict handling."""

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import uuid
from typing import Any

from .storage import SQLiteStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _decode(value: str) -> Any:
    return json.loads(value)


class SyncEngine:
    """Implements the API's persistence, merge, and conflict rules."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def create_document(self, device_id: str, data: dict[str, Any]) -> dict[str, Any]:
        document_id = str(uuid.uuid4())
        now = _now()
        data_json = _json(data)
        fields_json = _json(sorted(data.keys()))

        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO documents (id, data_json, version, created_at, updated_at)
                   VALUES (?, ?, 1, ?, ?)""",
                (document_id, data_json, now, now),
            )
            connection.execute(
                """INSERT INTO changes
                   (document_id, version, device_id, operation_id, base_version,
                    changed_fields_json, changes_json, created_at)
                   VALUES (?, 1, ?, NULL, 0, ?, ?, ?)""",
                (document_id, device_id, fields_json, data_json, now),
            )

        return {
            "document_id": document_id,
            "version": 1,
            "data": data,
            "created_at": now,
        }

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT id, data_json, version, created_at, updated_at FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()

        if row is None:
            return None
        return {
            "document_id": row["id"],
            "version": row["version"],
            "data": _decode(row["data_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_history(self, document_id: str, after_version: int, limit: int) -> list[dict[str, Any]] | None:
        with self.store.connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if exists is None:
                return None
            rows = connection.execute(
                """SELECT version, device_id, operation_id, base_version,
                          changed_fields_json, changes_json, created_at
                   FROM changes
                   WHERE document_id = ? AND version > ?
                   ORDER BY version ASC LIMIT ?""",
                (document_id, after_version, limit),
            ).fetchall()

        return [
            {
                "version": row["version"],
                "device_id": row["device_id"],
                "operation_id": row["operation_id"],
                "base_version": row["base_version"],
                "changed_fields": _decode(row["changed_fields_json"]),
                "changes": _decode(row["changes_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def sync(
        self,
        document_id: str,
        device_id: str,
        operation_id: str,
        base_version: int,
        changes: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        request_data = {
            "document_id": document_id,
            "device_id": device_id,
            "base_version": base_version,
            "changes": changes,
        }
        request_hash = hashlib.sha256(_json(request_data).encode("utf-8")).hexdigest()

        with self.store.transaction() as connection:
            previous_operation = connection.execute(
                """SELECT request_hash, http_status, response_json
                   FROM sync_operations WHERE document_id = ? AND operation_id = ?""",
                (document_id, operation_id),
            ).fetchone()
            if previous_operation is not None:
                if previous_operation["request_hash"] != request_hash:
                    return 409, {
                        "error": "idempotency_key_reused",
                        "message": "This operation_id was already used for a different request.",
                    }
                replay = _decode(previous_operation["response_json"])
                replay["replayed"] = True
                return previous_operation["http_status"], replay

            document = connection.execute(
                "SELECT data_json, version FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if document is None:
                return 404, {"error": "document_not_found", "document_id": document_id}

            current_version = document["version"]
            current_data = _decode(document["data_json"])

            if base_version > current_version:
                body = {
                    "outcome": "REJECTED",
                    "reason": "base_version_is_in_the_future",
                    "document_id": document_id,
                    "base_version": base_version,
                    "current_version": current_version,
                    "current_data": current_data,
                }
                self._remember_operation(
                    connection, document_id, operation_id, request_hash, 409, body
                )
                return 409, body

            fields_changed_since_base: set[str] = set()
            if base_version < current_version:
                event_rows = connection.execute(
                    """SELECT changed_fields_json FROM changes
                       WHERE document_id = ? AND version > ? ORDER BY version ASC""",
                    (document_id, base_version),
                ).fetchall()
                for event in event_rows:
                    fields_changed_since_base.update(_decode(event["changed_fields_json"]))

            conflicts = sorted(fields_changed_since_base.intersection(changes.keys()))
            if conflicts:
                body = {
                    "outcome": "CONFLICT",
                    "document_id": document_id,
                    "base_version": base_version,
                    "current_version": current_version,
                    "conflicting_fields": conflicts,
                    "current_data": current_data,
                    "message": "Fetch the current document, resolve these fields, and submit a new operation_id.",
                }
                self._remember_operation(
                    connection, document_id, operation_id, request_hash, 409, body
                )
                return 409, body

            updated_data = dict(current_data)
            updated_data.update(changes)
            updated_version = current_version + 1
            now = _now()
            cursor = connection.execute(
                """UPDATE documents
                   SET data_json = ?, version = ?, updated_at = ?
                   WHERE id = ? AND version = ?""",
                (_json(updated_data), updated_version, now, document_id, current_version),
            )
            if cursor.rowcount != 1:
                raise sqlite3.OperationalError("The document changed during an atomic sync operation.")

            connection.execute(
                """INSERT INTO changes
                   (document_id, version, device_id, operation_id, base_version,
                    changed_fields_json, changes_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    document_id,
                    updated_version,
                    device_id,
                    operation_id,
                    base_version,
                    _json(sorted(changes.keys())),
                    _json(changes),
                    now,
                ),
            )

            outcome = "ACCEPTED" if base_version == current_version else "MERGED"
            body = {
                "outcome": outcome,
                "document_id": document_id,
                "base_version": base_version,
                "applied_version": updated_version,
                "current_version": updated_version,
                "applied_fields": sorted(changes.keys()),
                "data": updated_data,
            }
            self._remember_operation(
                connection, document_id, operation_id, request_hash, 200, body
            )
            return 200, body

    @staticmethod
    def _remember_operation(
        connection: sqlite3.Connection,
        document_id: str,
        operation_id: str,
        request_hash: str,
        http_status: int,
        response: dict[str, Any],
    ) -> None:
        connection.execute(
            """INSERT INTO sync_operations
               (document_id, operation_id, request_hash, http_status, response_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (document_id, operation_id, request_hash, http_status, _json(response), _now()),
        )

