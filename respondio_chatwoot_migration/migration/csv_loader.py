"""Read the respond.io CSV exports.

The file *kind* (contacts / conversations / messages) is detected from the
header row, so file names do not matter. Column names are the exact ones found
in the exports; a file missing a required column is rejected loudly.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .models import MediaRef, SourceContact, SourceConversation, SourceMessage

csv.field_size_limit(1_000_000_000)

CONTACT_COLUMNS = {"ContactID", "FirstName", "LastName", "PhoneNumber", "Email"}
CONTACT_OPTIONAL = [
    "Country",
    "Language",
    "Tags",
    "Status",
    "Lifecycle",
    "Assignee",
    "Collaborators",
    "LastInteractionTime",
    "DateTimeCreated",
    "Channels",
]
CONVERSATION_COLUMNS = {
    "Conversation ID",
    "DateTime Conversation Started",
    "DateTime Conversation Resolved",
    "Contact ID",
}
MESSAGE_COLUMNS = {
    "Date & Time",
    "Sender ID",
    "Sender Type",
    "Contact ID",
    "Message ID",
    "Content Type",
    "Message Type",
    "Content",
    "Channel ID",
}

DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S")


class CsvSchemaError(Exception):
    pass


@dataclass
class FileInfo:
    path: Path
    kind: str
    columns: list[str]
    rows: int


@dataclass
class RawData:
    files: list[FileInfo] = field(default_factory=list)
    contacts: list[SourceContact] = field(default_factory=list)
    conversations: list[SourceConversation] = field(default_factory=list)
    messages: list[SourceMessage] = field(default_factory=list)
    parse_errors: list[tuple[str, int, str]] = field(default_factory=list)


def detect_kind(columns: list[str]) -> Optional[str]:
    cols = set(columns)
    if MESSAGE_COLUMNS <= cols:
        return "messages"
    if CONVERSATION_COLUMNS <= cols:
        return "conversations"
    if CONTACT_COLUMNS <= cols:
        return "contacts"
    return None


def parse_local_datetime(value: str, tz: ZoneInfo) -> Optional[datetime]:
    """Parse a naive export timestamp expressed in the source timezone and return UTC."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in DATETIME_FORMATS:
        try:
            naive = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=tz).astimezone(timezone.utc)
    raise ValueError(f"unrecognised datetime {value!r}")


def _to_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def extract_media(payload: dict[str, Any]) -> Optional[MediaRef]:
    """Pull the media reference out of a respond.io message payload, if any."""
    ptype = payload.get("type")
    node = None
    kind = None
    if ptype == "attachment" and isinstance(payload.get("attachment"), dict):
        node = payload["attachment"]
        kind = {"image": "image", "video": "video", "audio": "audio", "file": "file"}.get(
            str(node.get("type", "")).lower(), "file"
        )
    elif ptype == "sticker" and isinstance(payload.get("sticker"), dict):
        node = payload["sticker"]
        kind = "sticker"
    elif ptype == "whatsapp_template" and isinstance(payload.get("template"), dict):
        # Media header inside a template (rare): keep the first URL we can find.
        for comp in payload["template"].get("components", []) or []:
            if str(comp.get("type", "")).lower() != "header":
                continue
            for key in ("url", "link"):
                if comp.get(key):
                    node = {"url": comp[key], "type": comp.get("format", "file"), "fileName": comp.get("fileName", "")}
                    kind = str(node["type"]).lower()
                    if kind not in {"image", "video", "audio"}:
                        kind = "file"
                    break
            for param in comp.get("parameters", []) or []:
                for sub in ("image", "video", "document"):
                    if isinstance(param.get(sub), dict) and param[sub].get("link"):
                        node = {"url": param[sub]["link"], "type": sub, "fileName": param[sub].get("filename", "")}
                        kind = "file" if sub == "document" else sub
                        break
            if node:
                break
    if not node or not node.get("url"):
        return None
    mime = node.get("mimeType") or node.get("mime") or ""
    return MediaRef(
        url=str(node["url"]),
        kind=kind or "file",
        mime=str(mime),
        file_name=str(node.get("fileName") or ""),
        ext=str(node.get("ext") or ""),
        size=_to_int(node.get("size")),
    )


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        columns = list(reader.fieldnames or [])
        rows = list(reader)
    return columns, rows


def load_data_dir(data_dir: Path, tz: ZoneInfo, logger: Optional[logging.Logger] = None) -> RawData:
    log = logger or logging.getLogger("migration")
    if not data_dir.exists():
        raise CsvSchemaError(f"data directory {data_dir} does not exist")
    paths = sorted(p for p in data_dir.iterdir() if p.suffix.lower() == ".csv")
    if not paths:
        raise CsvSchemaError(f"no .csv files found in {data_dir}")

    raw = RawData()
    for path in paths:
        columns, rows = _read_rows(path)
        kind = detect_kind(columns)
        if kind is None:
            raise CsvSchemaError(
                f"{path.name}: cannot tell whether this is a contacts, conversations or messages export. Columns: {columns}"
            )
        raw.files.append(FileInfo(path=path, kind=kind, columns=columns, rows=len(rows)))
        log.info("Loaded %s as %s export (%d rows)", path.name, kind, len(rows))
        if kind == "contacts":
            raw.contacts.extend(_parse_contacts(path, rows, tz, raw))
        elif kind == "conversations":
            raw.conversations.extend(_parse_conversations(path, rows, tz, raw))
        else:
            raw.messages.extend(_parse_messages(path, rows, tz, raw))

    kinds = {f.kind for f in raw.files}
    for needed in ("contacts", "conversations", "messages"):
        if needed not in kinds:
            log.warning("No %s export found in %s", needed, data_dir)
    return raw


def _parse_contacts(path: Path, rows: list[dict[str, str]], tz: ZoneInfo, raw: RawData) -> list[SourceContact]:
    out = []
    for idx, row in enumerate(rows, start=2):
        cid = (row.get("ContactID") or "").strip()
        if not cid:
            raw.parse_errors.append((path.name, idx, "empty ContactID"))
            continue
        try:
            created = parse_local_datetime(row.get("DateTimeCreated", ""), tz)
            last = parse_local_datetime(row.get("LastInteractionTime", ""), tz)
        except ValueError as exc:
            raw.parse_errors.append((path.name, idx, str(exc)))
            created = last = None
        known = CONTACT_COLUMNS | set(CONTACT_OPTIONAL)
        extra = {k: (v or "") for k, v in row.items() if k and k not in known and (v or "").strip()}
        out.append(
            SourceContact(
                contact_id=cid,
                first_name=(row.get("FirstName") or "").strip(),
                last_name=(row.get("LastName") or "").strip(),
                phone_raw=(row.get("PhoneNumber") or "").strip(),
                email=(row.get("Email") or "").strip().lower(),
                country=(row.get("Country") or "").strip(),
                language=(row.get("Language") or "").strip(),
                tags=(row.get("Tags") or "").strip(),
                status=(row.get("Status") or "").strip(),
                lifecycle=(row.get("Lifecycle") or "").strip(),
                assignee=(row.get("Assignee") or "").strip(),
                channels=(row.get("Channels") or "").strip(),
                created_at=created,
                last_interaction_at=last,
                extra=extra,
                source_file=path.name,
                row_number=idx,
            )
        )
    return out


def _parse_conversations(
    path: Path, rows: list[dict[str, str]], tz: ZoneInfo, raw: RawData
) -> list[SourceConversation]:
    out = []
    for idx, row in enumerate(rows, start=2):
        conv_id = (row.get("Conversation ID") or "").strip()
        contact_id = (row.get("Contact ID") or "").strip()
        if not conv_id or not contact_id:
            raw.parse_errors.append((path.name, idx, "empty Conversation ID or Contact ID"))
            continue
        try:
            started = parse_local_datetime(row.get("DateTime Conversation Started", ""), tz)
            resolved = parse_local_datetime(row.get("DateTime Conversation Resolved", ""), tz)
        except ValueError as exc:
            raw.parse_errors.append((path.name, idx, str(exc)))
            continue
        if started is None:
            raw.parse_errors.append((path.name, idx, "missing DateTime Conversation Started"))
            continue
        out.append(
            SourceConversation(
                conversation_id=conv_id,
                contact_id=contact_id,
                started_at=started,
                resolved_at=resolved,
                started_local=(row.get("DateTime Conversation Started") or "").strip(),
                resolved_local=(row.get("DateTime Conversation Resolved") or "").strip(),
                assignee=(row.get("Assignee") or "").strip(),
                closed_by=(row.get("Closed By") or "").strip(),
                category=(row.get("Conversation Category") or "").strip(),
                closing_note=(row.get("Closing Note Summary") or "").strip(),
                opened_by_channel=(row.get("Opened By Channel") or "").strip(),
                n_incoming=_to_int(row.get("Number of Incoming Messages")),
                n_outgoing=_to_int(row.get("Number of Outgoing Messages")),
                source_file=path.name,
                row_number=idx,
            )
        )
    return out


def _parse_messages(path: Path, rows: list[dict[str, str]], tz: ZoneInfo, raw: RawData) -> list[SourceMessage]:
    out = []
    for idx, row in enumerate(rows, start=2):
        mid = (row.get("Message ID") or "").strip()
        contact_id = (row.get("Contact ID") or "").strip()
        if not mid or not contact_id:
            raw.parse_errors.append((path.name, idx, "empty Message ID or Contact ID"))
            continue
        try:
            ts = parse_local_datetime(row.get("Date & Time", ""), tz)
        except ValueError as exc:
            raw.parse_errors.append((path.name, idx, str(exc)))
            continue
        if ts is None:
            raw.parse_errors.append((path.name, idx, "missing Date & Time"))
            continue
        direction = (row.get("Message Type") or "").strip().lower()
        if direction not in {"incoming", "outgoing"}:
            raw.parse_errors.append((path.name, idx, f"unknown Message Type {direction!r}"))
            continue
        raw_content = row.get("Content") or ""
        try:
            payload = json.loads(raw_content) if raw_content.strip() else {}
            if not isinstance(payload, dict):
                payload = {"type": "raw", "text": raw_content}
        except json.JSONDecodeError:
            payload = {"type": "raw", "text": raw_content}
        out.append(
            SourceMessage(
                message_id=mid,
                contact_id=contact_id,
                sender_id=(row.get("Sender ID") or "").strip(),
                sender_type=(row.get("Sender Type") or "").strip(),
                content_type=(row.get("Content Type") or "").strip() or str(payload.get("type", "")),
                direction=direction,
                raw_content=raw_content,
                payload=payload,
                channel_id=(row.get("Channel ID") or "").strip(),
                type_col=(row.get("Type") or "").strip(),
                sub_type=(row.get("Sub Type") or "").strip(),
                timestamp=ts,
                local_time=(row.get("Date & Time") or "").strip(),
                media=extract_media(payload),
                source_file=path.name,
                row_number=idx,
            )
        )
    return out
