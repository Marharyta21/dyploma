"""A small in-memory Chatwoot look-alike used by the tests (and for local demos).

It implements only the endpoints the migration uses, with the same request and
response shapes as Chatwoot's Application API, plus a /media/<name> route that
serves files from a directory so media downloads can be exercised offline.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse


class FakeChatwoot:
    def __init__(self, account_id: int = 185855, media_dir: Optional[Path] = None, token: str = "secret-token"):
        self.account_id = account_id
        self.token = token
        self.media_dir = media_dir
        self.lock = threading.Lock()
        self.inboxes: dict[int, dict] = {
            145492: {"id": 145492, "name": "Respond.io Archive", "channel_type": "Channel::Api", "lock_to_single_conversation": False, "callback_webhook_url": None},
            100001: {"id": 100001, "name": "Big Moe Watches WhatsApp", "channel_type": "Channel::Whatsapp", "phone_number": "+971500000000"},
        }
        self.contacts: dict[int, dict] = {}
        self.contact_inboxes: list[dict] = []
        self.conversations: dict[int, dict] = {}
        self.messages: dict[int, dict] = {}
        self.custom_attribute_definitions: list[dict] = []
        self.requests: list[tuple[str, str]] = []
        self.flood_limit: Optional[int] = None  # emulate "Too many messages" after N messages per conversation
        self._next = {"contact": 1000, "conversation": 500, "message": 90000, "cad": 1}
        self.server: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _send(self, status: int, body: Any = None, content_type: str = "application/json"):
                data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def _body(self) -> tuple[dict, dict]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                ctype = self.headers.get("Content-Type", "")
                if ctype.startswith("application/json"):
                    return (json.loads(raw) if raw else {}), {}
                if ctype.startswith("multipart/form-data"):
                    msg = BytesParser(policy=email_policy).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
                    fields: dict = {}
                    files: dict = {}
                    for part in msg.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        filename = part.get_filename()
                        payload = part.get_payload(decode=True)
                        if filename:
                            files.setdefault(name, []).append({"filename": filename, "content_type": part.get_content_type(), "size": len(payload)})
                        else:
                            fields[name] = payload.decode()
                    return fields, files
                return {}, {}

            def do_GET(self):
                fake.handle(self, "GET")

            def do_POST(self):
                fake.handle(self, "POST")

            def do_PUT(self):
                fake.handle(self, "PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    # ------------------------------------------------------------ helpers
    def _id(self, kind: str) -> int:
        self._next[kind] += 1
        return self._next[kind]

    def _contact_json(self, c: dict) -> dict:
        cis = [
            {"source_id": ci["source_id"], "inbox": self.inboxes[ci["inbox_id"]]}
            for ci in self.contact_inboxes
            if ci["contact_id"] == c["id"]
        ]
        return {**c, "contact_inboxes": cis}

    def _conversation_json(self, conv: dict) -> dict:
        return {k: v for k, v in conv.items()}

    # ------------------------------------------------------------ routing
    def handle(self, h, method: str) -> None:
        url = urlparse(h.path)
        path = url.path
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        with self.lock:
            self.requests.append((method, path))
        if path.startswith("/media/"):
            if self.media_dir is None:
                return h._send(404, {"error": "no media"})
            f = self.media_dir / path[len("/media/"):]
            if not f.exists():
                return h._send(404, b"not found", "text/plain")
            return h._send(200, f.read_bytes(), "application/octet-stream")

        if h.headers.get("api_access_token") != self.token:
            return h._send(401, {"error": "Unauthorized"})

        if path == "/api/v1/profile" and method == "GET":
            return h._send(200, {"id": 7, "name": "Migration Bot", "email": "bot@example.com", "accounts": [{"id": self.account_id, "name": "Big Moe", "role": "administrator"}]})

        prefix = f"/api/v1/accounts/{self.account_id}"
        if not path.startswith(prefix):
            return h._send(404, {"error": "Resource could not be found"})
        rest = path[len(prefix):]

        with self.lock:
            # inboxes
            if rest == "/inboxes" and method == "GET":
                return h._send(200, {"payload": list(self.inboxes.values())})
            m = re.fullmatch(r"/inboxes/(\d+)", rest)
            if m and method == "GET":
                inbox = self.inboxes.get(int(m.group(1)))
                return h._send(200, inbox) if inbox else h._send(404, {"error": "Resource could not be found"})

            # custom attribute definitions
            if rest == "/custom_attribute_definitions":
                if method == "GET":
                    model = qs.get("attribute_model")
                    return h._send(200, [d for d in self.custom_attribute_definitions if model in (None, d["attribute_model"], str(d["attribute_model_int"]))])
                body, _ = h._body()
                d = {"id": self._id("cad"), **body, "attribute_model": "contact_attribute" if body.get("attribute_model") in (1, "1", "contact_attribute") else "conversation_attribute", "attribute_model_int": body.get("attribute_model")}
                self.custom_attribute_definitions.append(d)
                return h._send(200, d)

            # contacts
            if rest == "/contacts/search" and method == "GET":
                q = qs.get("q", "").strip().lower()
                hits = [self._contact_json(c) for c in self.contacts.values() if q and any(q in str(c.get(k) or "").lower() for k in ("name", "email", "phone_number", "identifier"))]
                return h._send(200, {"meta": {"count": len(hits)}, "payload": hits})
            if rest == "/contacts" and method == "POST":
                body, _ = h._body()
                phone = body.get("phone_number")
                if phone and not re.fullmatch(r"\+[1-9]\d{1,14}", phone):
                    return h._send(422, {"message": "Phone number should be in e164 format", "attributes": ["phone_number"]})
                for c in self.contacts.values():
                    if phone and c.get("phone_number") == phone:
                        return h._send(422, {"message": "Phone number has already been taken", "attributes": ["phone_number"]})
                    if body.get("identifier") and c.get("identifier") == body["identifier"]:
                        return h._send(422, {"message": "Identifier has already been taken", "attributes": ["identifier"]})
                    if body.get("email") and c.get("email") == body["email"]:
                        return h._send(422, {"message": "Email has already been taken", "attributes": ["email"]})
                cid = self._id("contact")
                c = {
                    "id": cid,
                    "name": body.get("name", ""),
                    "email": body.get("email"),
                    "phone_number": phone,
                    "identifier": body.get("identifier"),
                    "custom_attributes": body.get("custom_attributes") or {},
                    "additional_attributes": body.get("additional_attributes") or {},
                }
                self.contacts[cid] = c
                ci = None
                if body.get("inbox_id"):
                    inbox_id = int(body["inbox_id"])
                    if inbox_id not in self.inboxes:
                        return h._send(404, {"error": "Resource could not be found"})
                    ci = {"contact_id": cid, "inbox_id": inbox_id, "source_id": str(uuid.uuid4())}
                    self.contact_inboxes.append(ci)
                return h._send(200, {"payload": {"contact": self._contact_json(c), "contact_inbox": {"source_id": ci["source_id"], "inbox": self.inboxes[ci["inbox_id"]]} if ci else None}})
            m = re.fullmatch(r"/contacts/(\d+)", rest)
            if m:
                c = self.contacts.get(int(m.group(1)))
                if not c:
                    return h._send(404, {"error": "Resource could not be found"})
                if method == "GET":
                    return h._send(200, {"payload": self._contact_json(c)})
                if method == "PUT":
                    body, _ = h._body()
                    for k in ("name", "email", "phone_number", "identifier"):
                        if k in body:
                            c[k] = body[k]
                    if "custom_attributes" in body:
                        c["custom_attributes"] = {**c["custom_attributes"], **body["custom_attributes"]}
                    if "additional_attributes" in body:
                        c["additional_attributes"] = {**c["additional_attributes"], **body["additional_attributes"]}
                    return h._send(200, {"payload": self._contact_json(c)})
            m = re.fullmatch(r"/contacts/(\d+)/contact_inboxes", rest)
            if m and method == "POST":
                c = self.contacts.get(int(m.group(1)))
                body, _ = h._body()
                inbox = self.inboxes.get(int(body.get("inbox_id", 0)))
                if not c or not inbox:
                    return h._send(404, {"error": "Resource could not be found"})
                source_id = body.get("source_id") or str(uuid.uuid4())
                existing = next((ci for ci in self.contact_inboxes if ci["contact_id"] == c["id"] and ci["inbox_id"] == inbox["id"] and ci["source_id"] == source_id), None)
                if not existing:
                    existing = {"contact_id": c["id"], "inbox_id": inbox["id"], "source_id": source_id}
                    self.contact_inboxes.append(existing)
                return h._send(200, {"source_id": existing["source_id"], "inbox": inbox})
            m = re.fullmatch(r"/contacts/(\d+)/conversations", rest)
            if m and method == "GET":
                cid = int(m.group(1))
                convs = [self._conversation_json(c) for c in self.conversations.values() if c["contact_id"] == cid]
                return h._send(200, {"payload": convs[-25:]})

            # conversations
            if rest == "/conversations" and method == "POST":
                body, _ = h._body()
                inbox = self.inboxes.get(int(body.get("inbox_id", 0)))
                if not inbox:
                    return h._send(404, {"error": "Resource could not be found"})
                ci = next((x for x in self.contact_inboxes if x["source_id"] == body.get("source_id") and x["inbox_id"] == inbox["id"]), None)
                if not ci:
                    return h._send(404, {"error": "Resource could not be found"})
                conv_id = self._id("conversation")
                conv = {
                    "id": conv_id,
                    "inbox_id": inbox["id"],
                    "contact_id": ci["contact_id"],
                    "status": body.get("status", "open"),
                    "additional_attributes": body.get("additional_attributes") or {},
                    "custom_attributes": body.get("custom_attributes") or {},
                    "messages": [],
                }
                self.conversations[conv_id] = conv
                return h._send(200, self._conversation_json(conv))
            m = re.fullmatch(r"/conversations/(\d+)", rest)
            if m and method == "GET":
                conv = self.conversations.get(int(m.group(1)))
                return h._send(200, self._conversation_json(conv)) if conv else h._send(404, {"error": "Resource could not be found"})
            m = re.fullmatch(r"/conversations/(\d+)/toggle_status", rest)
            if m and method == "POST":
                conv = self.conversations.get(int(m.group(1)))
                if not conv:
                    return h._send(404, {"error": "Resource could not be found"})
                body, _ = h._body()
                conv["status"] = body.get("status") or ("open" if conv["status"] == "resolved" else "resolved")
                return h._send(200, {"payload": {"success": True, "current_status": conv["status"], "conversation_id": conv["id"]}})
            m = re.fullmatch(r"/conversations/(\d+)/messages", rest)
            if m:
                conv = self.conversations.get(int(m.group(1)))
                if not conv:
                    return h._send(404, {"error": "Resource could not be found"})
                if method == "GET":
                    msgs = sorted((x for x in self.messages.values() if x["conversation_id"] == conv["id"]), key=lambda x: x["id"])
                    before = qs.get("before")
                    if before:
                        msgs = [x for x in msgs if x["id"] < int(before)]
                    return h._send(200, {"meta": {}, "payload": msgs[-20:]})
                body, files = h._body()
                if self.flood_limit is not None:
                    n = sum(1 for x in self.messages.values() if x["conversation_id"] == conv["id"])
                    if n >= self.flood_limit:
                        self.flood_limit = None  # succeed on retry
                        return h._send(422, {"message": "Too many messages"})
                mtype = body.get("message_type", "outgoing")
                if mtype not in ("incoming", "outgoing"):
                    return h._send(422, {"message": "invalid message_type"})
                attrs = body.get("content_attributes") or {}
                if isinstance(attrs, str):
                    attrs = json.loads(attrs) if attrs else {}
                if body.get("external_created_at"):
                    attrs = {**attrs, "external_created_at": body["external_created_at"]}
                mid = self._id("message")
                msg = {
                    "id": mid,
                    "conversation_id": conv["id"],
                    "content": body.get("content"),
                    "message_type": 0 if mtype == "incoming" else 1,
                    "private": str(body.get("private", "false")).lower() == "true",
                    "content_type": body.get("content_type") or "text",
                    "content_attributes": attrs,
                    "source_id": body.get("source_id"),
                    "created_at": 1_800_000_000 + mid,
                    "attachments": [
                        {"id": mid * 10 + i, "file_type": f["content_type"].split("/")[0], "file_name": f["filename"], "file_size": f["size"]}
                        for i, f in enumerate(files.get("attachments[]", []))
                    ],
                }
                self.messages[mid] = msg
                if mtype == "incoming" and conv["status"] == "resolved":
                    conv["status"] = "open"  # Chatwoot reopens resolved conversations on incoming messages
                return h._send(200, msg)

        return h._send(404, {"error": "Resource could not be found"})


def serve_forever(port: int = 8765) -> None:  # pragma: no cover - manual rehearsal helper
    """Run the fake server on a fixed port so the CLI can be rehearsed offline."""
    import time

    server = FakeChatwoot()
    server.start()
    # rebind on the requested port for a stable URL
    server.stop()
    server.server = ThreadingHTTPServer(("127.0.0.1", port), server.server.RequestHandlerClass)
    server.thread = threading.Thread(target=server.server.serve_forever, daemon=True)
    server.thread.start()
    print(f"Fake Chatwoot listening on http://127.0.0.1:{port}  (account {server.account_id}, token {server.token!r}, "
          f"archive inbox 145492, WhatsApp inbox 100001). Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    import sys

    serve_forever(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
