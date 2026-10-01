"""Small JSON HTTP API built with Python's standard library."""

from __future__ import annotations

from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .engine import SyncEngine


MAX_BODY_BYTES = 1_000_000
MAX_HISTORY_LIMIT = 500


class RequestError(Exception):
    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"{value} is not a valid JSON number")


def _text(payload: dict[str, Any], key: str, max_length: int = 128) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RequestError(400, "invalid_request", f"{key} must be a non-empty string.")
    value = value.strip()
    if len(value) > max_length:
        raise RequestError(400, "invalid_request", f"{key} must be at most {max_length} characters.")
    return value


def _object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RequestError(400, "invalid_request", f"{key} must be a JSON object.")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise RequestError(400, "invalid_request", f"{key} contains a value that is not valid JSON.") from None
    return value


def _only_fields(payload: dict[str, Any], allowed: set[str]) -> None:
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        raise RequestError(
            400,
            "invalid_request",
            "Unexpected field(s): " + ", ".join(unexpected),
        )


def _changed_fields(payload: dict[str, Any]) -> dict[str, Any]:
    changes = _object(payload, "changes")
    if not changes:
        raise RequestError(400, "invalid_request", "changes must contain at least one field.")
    for field in changes:
        if not isinstance(field, str) or not field.strip() or field != field.strip():
            raise RequestError(
                400,
                "invalid_request",
                "Each changes key must be a non-empty field name without surrounding whitespace.",
            )
        if len(field) > 128:
            raise RequestError(400, "invalid_request", "Change field names must be at most 128 characters.")
    return changes


class SyncRequestHandler(BaseHTTPRequestHandler):
    server_version = "OfflineSyncDemo/1.0"
    sys_version = ""

    def __init__(self, *args: Any, engine: SyncEngine, **kwargs: Any) -> None:
        self.engine = engine
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        try:
            parsed = urlsplit(self.path)
            parts = [part for part in parsed.path.strip("/").split("/") if part]

            if parts == ["healthz"]:
                self._send_json(200, {"status": "ok"})
                return

            if len(parts) == 2 and parts[0] == "documents":
                document = self.engine.get_document(parts[1])
                if document is None:
                    self._send_json(404, {"error": "document_not_found", "document_id": parts[1]})
                else:
                    self._send_json(200, document)
                return

            if len(parts) == 3 and parts[0] == "documents" and parts[2] == "history":
                after_version, limit = self._history_query(parsed.query)
                history = self.engine.get_history(parts[1], after_version, limit)
                if history is None:
                    self._send_json(404, {"error": "document_not_found", "document_id": parts[1]})
                else:
                    self._send_json(200, {"document_id": parts[1], "changes": history})
                return

            self._send_json(404, {"error": "route_not_found"})
        except RequestError as exc:
            self._send_json(exc.status, {"error": exc.error, "message": exc.message})
        except sqlite3.OperationalError:
            self.log_error("Database remained busy while serving GET request")
            self._send_json(503, {"error": "database_busy", "message": "Retry the request shortly."})
        except Exception:
            self.log_error("Unexpected error while serving GET request")
            self._send_json(500, {"error": "internal_error", "message": "The server could not complete the request."})

    def do_POST(self) -> None:
        try:
            parsed = urlsplit(self.path)
            parts = [part for part in parsed.path.strip("/").split("/") if part]
            payload = self._read_json_object()

            if parts == ["documents"]:
                _only_fields(payload, {"device_id", "data"})
                device_id = _text(payload, "device_id")
                data = _object(payload, "data")
                self._send_json(201, self.engine.create_document(device_id, data))
                return

            if len(parts) == 3 and parts[0] == "documents" and parts[2] == "sync":
                _only_fields(payload, {"device_id", "operation_id", "base_version", "changes"})
                device_id = _text(payload, "device_id")
                operation_id = _text(payload, "operation_id")
                base_version = payload.get("base_version")
                if type(base_version) is not int or base_version < 0:
                    raise RequestError(400, "invalid_request", "base_version must be a non-negative integer.")
                changes = _changed_fields(payload)
                status, body = self.engine.sync(
                    parts[1], device_id, operation_id, base_version, changes
                )
                self._send_json(status, body)
                return

            self._send_json(404, {"error": "route_not_found"})
        except RequestError as exc:
            self._send_json(exc.status, {"error": exc.error, "message": exc.message})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "invalid_json", "message": "Request body must be a valid JSON object."})
        except sqlite3.OperationalError:
            self.log_error("Database remained busy while serving POST request")
            self._send_json(503, {"error": "database_busy", "message": "Retry the request shortly."})
        except Exception:
            self.log_error("Unexpected error while serving POST request")
            self._send_json(500, {"error": "internal_error", "message": "The server could not complete the request."})

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_HEAD(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._send_json(
            405,
            {"error": "method_not_allowed", "message": "Use the method listed in the endpoint documentation."},
            extra_headers={"Allow": "GET, POST"},
        )

    def _read_json_object(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise RequestError(415, "unsupported_media_type", "Set Content-Type to application/json.")

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise RequestError(411, "length_required", "Content-Length is required.")
        try:
            length = int(raw_length)
        except ValueError:
            raise RequestError(400, "invalid_content_length", "Content-Length must be an integer.") from None
        if length < 0:
            raise RequestError(400, "invalid_content_length", "Content-Length cannot be negative.")
        if length > MAX_BODY_BYTES:
            raise RequestError(413, "request_too_large", f"Request bodies are limited to {MAX_BODY_BYTES} bytes.")

        raw_body = self.rfile.read(length)
        if len(raw_body) != length:
            raise RequestError(400, "incomplete_body", "The request body ended before Content-Length bytes arrived.")
        try:
            payload = json.loads(raw_body.decode("utf-8"), parse_constant=_reject_non_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise RequestError(400, "invalid_json", "Request body must be valid UTF-8 JSON.") from None
        if not isinstance(payload, dict):
            raise RequestError(400, "invalid_request", "The top-level JSON value must be an object.")
        return payload

    @staticmethod
    def _history_query(query: str) -> tuple[int, int]:
        try:
            values = parse_qs(query, strict_parsing=True) if query else {}
        except ValueError:
            raise RequestError(400, "invalid_query", "Query parameters must use key=value format.") from None
        unexpected = sorted(set(values) - {"after_version", "limit"})
        if unexpected:
            raise RequestError(400, "invalid_query", "Unexpected query parameter(s): " + ", ".join(unexpected))
        if any(len(items) != 1 for items in values.values()):
            raise RequestError(400, "invalid_query", "Each query parameter may be provided only once.")

        after_version = 0
        limit = 100
        if "after_version" in values:
            raw = values["after_version"][0]
            if not raw.isdecimal():
                raise RequestError(400, "invalid_query", "after_version must be a non-negative integer.")
            after_version = int(raw)
        if "limit" in values:
            raw = values["limit"][0]
            if not raw.isdecimal() or int(raw) < 1 or int(raw) > MAX_HISTORY_LIMIT:
                raise RequestError(400, "invalid_query", f"limit must be between 1 and {MAX_HISTORY_LIMIT}.")
            limit = int(raw)
        return after_version, limit

    def _send_json(
        self,
        status: int,
        body: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format_string: str, *args: Any) -> None:
        # Keep request bodies and document contents out of routine logs.
        super().log_message(format_string, *args)


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server(host: str, port: int, engine: SyncEngine) -> ThreadingHTTPServer:
    handler: Callable[..., SyncRequestHandler] = partial(SyncRequestHandler, engine=engine)
    return ReusableThreadingHTTPServer((host, port), handler)

