"""Thin Chatwoot Application API client.

Only endpoints documented in the official OpenAPI spec
(https://github.com/chatwoot/chatwoot/blob/develop/swagger/swagger.json) are
used. All writes are blocked unless the client was created with
``allow_writes=True`` so that dry-run can never modify Chatwoot by accident.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

import requests

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ChatwootError(Exception):
    pass


class WriteBlocked(ChatwootError):
    """Raised when a write is attempted in read-only (dry-run) mode."""


class ChatwootHTTPError(ChatwootError):
    def __init__(self, method: str, path: str, status: int, body: str):
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:500]}")


class RateLimiter:
    """Simple sliding-window limiter: at most ``limit`` calls per ``period`` seconds."""

    def __init__(self, limit: int, period: float = 60.0):
        self.limit = max(1, limit)
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] > self.period:
                    self._calls.popleft()
                if len(self._calls) < self.limit:
                    self._calls.append(now)
                    return
                sleep_for = self.period - (now - self._calls[0]) + 0.05
            time.sleep(max(sleep_for, 0.05))


class ChatwootClient:
    def __init__(
        self,
        base_url: str,
        account_id: int,
        api_token: str,
        allow_writes: bool,
        requests_per_minute: int = 240,
        timeout: int = 60,
        max_retries: int = 6,
        logger: Optional[logging.Logger] = None,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.account_id = int(account_id)
        self.allow_writes = allow_writes
        self.timeout = timeout
        self.max_retries = max_retries
        self.log = logger or logging.getLogger("migration")
        self.session = session or requests.Session()
        self.session.headers.update({"api_access_token": api_token, "Accept": "application/json"})
        self.limiter = RateLimiter(requests_per_minute)
        self.write_count = 0
        self.request_count = 0

    # ------------------------------------------------------------------ core
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _acct(self, suffix: str) -> str:
        return f"/api/v1/accounts/{self.account_id}{suffix}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[list] = None,
    ) -> Any:
        method = method.upper()
        if method != "GET":
            if not self.allow_writes:
                raise WriteBlocked(f"refusing {method} {path}: client is read-only (dry-run)")
            self.write_count += 1
        attempt = 0
        while True:
            attempt += 1
            self.limiter.wait()
            self.request_count += 1
            try:
                if files:
                    for _, (name, fh, mime) in files:
                        fh.seek(0)
                resp = self.session.request(
                    method,
                    self._url(path),
                    params=params,
                    json=json_body,
                    data=data,
                    files=files,
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt > self.max_retries:
                    raise ChatwootError(f"{method} {path}: network error after {attempt - 1} retries: {exc}") from exc
                delay = self._backoff(attempt)
                self.log.warning("%s %s network error (%s); retry %d in %.1fs", method, path, exc, attempt, delay)
                time.sleep(delay)
                continue

            if resp.status_code in RETRYABLE_STATUS and attempt <= self.max_retries:
                delay = self._backoff(attempt, resp.headers.get("Retry-After"))
                self.log.warning("%s %s -> HTTP %d; retry %d in %.1fs", method, path, resp.status_code, attempt, delay)
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise ChatwootHTTPError(method, path, resp.status_code, resp.text)
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text

    @staticmethod
    def _backoff(attempt: int, retry_after: Optional[str] = None) -> float:
        if retry_after:
            try:
                return float(retry_after) + random.uniform(0, 1)
            except ValueError:
                pass
        return min(60.0, (2 ** attempt) + random.uniform(0, 1))

    # ------------------------------------------------------------- read side
    def get_profile(self) -> dict:
        return self._request("GET", "/api/v1/profile")

    def list_inboxes(self) -> list[dict]:
        data = self._request("GET", self._acct("/inboxes"))
        return data.get("payload", data) if isinstance(data, dict) else data

    def get_inbox(self, inbox_id: int) -> dict:
        return self._request("GET", self._acct(f"/inboxes/{int(inbox_id)}"))

    def search_contacts(self, query: str) -> list[dict]:
        data = self._request("GET", self._acct("/contacts/search"), params={"q": query})
        return data.get("payload", []) if isinstance(data, dict) else []

    def get_contact(self, contact_id: int) -> dict:
        data = self._request("GET", self._acct(f"/contacts/{int(contact_id)}"))
        return data.get("payload", data) if isinstance(data, dict) else data

    def list_contact_conversations(self, contact_id: int) -> list[dict]:
        data = self._request("GET", self._acct(f"/contacts/{int(contact_id)}/conversations"))
        return data.get("payload", []) if isinstance(data, dict) else []

    def get_conversation(self, conversation_id: int) -> dict:
        return self._request("GET", self._acct(f"/conversations/{int(conversation_id)}"))

    def list_all_messages(self, conversation_id: int) -> list[dict]:
        """Walk backwards through the conversation with the documented ``before`` cursor."""
        out: list[dict] = []
        before: Optional[int] = None
        seen: set[int] = set()
        while True:
            params = {"before": before} if before else None
            data = self._request("GET", self._acct(f"/conversations/{int(conversation_id)}/messages"), params=params)
            batch = data.get("payload", []) if isinstance(data, dict) else []
            new = [m for m in batch if m.get("id") not in seen]
            if not new:
                break
            for m in new:
                seen.add(m["id"])
            out.extend(new)
            if len(batch) < 20:
                break
            before = min(m["id"] for m in new)
        out.sort(key=lambda m: (m.get("created_at", 0), m.get("id", 0)))
        return out

    def list_custom_attribute_definitions(self, attribute_model: str) -> list[dict]:
        data = self._request("GET", self._acct("/custom_attribute_definitions"), params={"attribute_model": attribute_model})
        return data if isinstance(data, list) else data.get("payload", [])

    # ------------------------------------------------------------ write side
    def create_custom_attribute_definition(self, payload: dict) -> dict:
        return self._request("POST", self._acct("/custom_attribute_definitions"), json_body=payload)

    def create_contact(self, payload: dict) -> dict:
        """Returns ``{"contact": {...}, "contact_inbox": {...}}``."""
        data = self._request("POST", self._acct("/contacts"), json_body=payload)
        inner = data.get("payload", data) if isinstance(data, dict) else {}
        if isinstance(inner, dict) and "contact" in inner:
            return inner
        return {"contact": inner, "contact_inbox": None}

    def update_contact(self, contact_id: int, payload: dict) -> dict:
        data = self._request("PUT", self._acct(f"/contacts/{int(contact_id)}"), json_body=payload)
        return data.get("payload", data) if isinstance(data, dict) else data

    def create_contact_inbox(self, contact_id: int, inbox_id: int, source_id: str) -> dict:
        return self._request(
            "POST",
            self._acct(f"/contacts/{int(contact_id)}/contact_inboxes"),
            json_body={"inbox_id": int(inbox_id), "source_id": source_id},
        )

    def create_conversation(self, payload: dict) -> dict:
        return self._request("POST", self._acct("/conversations"), json_body=payload)

    def create_message(
        self,
        conversation_id: int,
        *,
        content: str,
        message_type: str,
        content_attributes: Optional[dict] = None,
        external_created_at: Optional[str] = None,
        source_id: Optional[str] = None,
        attachment_path: Optional[Path] = None,
        attachment_mime: Optional[str] = None,
        attachment_name: Optional[str] = None,
    ) -> dict:
        path = self._acct(f"/conversations/{int(conversation_id)}/messages")
        if attachment_path is None:
            body: dict[str, Any] = {
                "content": content,
                "message_type": message_type,
                "private": False,
                "content_type": "text",
            }
            if content_attributes:
                body["content_attributes"] = content_attributes
            if external_created_at:
                body["external_created_at"] = external_created_at
            if source_id:
                body["source_id"] = source_id
            return self._request("POST", path, json_body=body)

        data: dict[str, Any] = {
            "content": content,
            "message_type": message_type,
            "private": "false",
            "content_type": "text",
        }
        if content_attributes:
            data["content_attributes"] = json.dumps(content_attributes, ensure_ascii=False)
        if external_created_at:
            data["external_created_at"] = external_created_at
        if source_id:
            data["source_id"] = source_id
        with open(attachment_path, "rb") as fh:
            files = [("attachments[]", (attachment_name or attachment_path.name, fh, attachment_mime or "application/octet-stream"))]
            return self._request("POST", path, data=data, files=files)

    def toggle_status(self, conversation_id: int, status: str) -> dict:
        return self._request("POST", self._acct(f"/conversations/{int(conversation_id)}/toggle_status"), json_body={"status": status})
