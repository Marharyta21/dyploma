from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fake_chatwoot import FakeChatwoot  # noqa: E402

CONTACT_COLUMNS = ["ContactID", "FirstName", "LastName", "PhoneNumber", "Email", "Country", "Language", "Tags", "Status", "Lifecycle", "Assignee", "Collaborators", "LastInteractionTime", "DateTimeCreated", "Channels"]
CONV_COLUMNS = ["Conversation ID", "DateTime Conversation Started", "DateTime Conversation Resolved", "Contact ID", "Assignee", "Number of Outgoing Messages", "Number of Incoming Messages", "Closed By", "Opened By Channel"]
MSG_COLUMNS = ["Date & Time", "Sender ID", "Sender Type", "Contact ID", "Message ID", "Content Type", "Message Type", "Content", "Channel ID", "Type", "Sub Type"]


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, quoting=csv.QUOTE_ALL)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})


def msg(ts, contact, mid, ctype, direction, payload, sender_type=None, sender=None, channel="437642"):
    return {
        "Date & Time": ts,
        "Sender ID": sender if sender is not None else (contact if direction == "incoming" else ""),
        "Sender Type": sender_type or ("contact" if direction == "incoming" else "n8n"),
        "Contact ID": contact,
        "Message ID": mid,
        "Content Type": ctype,
        "Message Type": direction,
        "Content": json.dumps(payload, ensure_ascii=False),
        "Channel ID": channel,
    }


ENV_KEYS = [
    "CHATWOOT_BASE_URL",
    "CHATWOOT_ACCOUNT_ID",
    "CHATWOOT_API_TOKEN",
    "CHATWOOT_ARCHIVE_INBOX_ID",
    "CHATWOOT_ARCHIVE_INBOX_NAME",
    "SOURCE_TIMEZONE",
    "DATA_DIR",
    "MEDIA_DIR",
    "OUTPUT_DIR",
    "REQUESTS_PER_MINUTE",
    "CONVERSATION_MESSAGES_PER_MINUTE",
    "TIMESTAMP_PREFIX",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """load_dotenv never overrides existing variables, so scrub them between tests."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    d = tmp_path / "srcmedia"
    d.mkdir()
    (d / "photo.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 500)
    (d / "voice.ogg").write_bytes(b"OggS" + b"y" * 300)
    (d / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftyp" + b"z" * 800)
    (d / "doc.pdf").write_bytes(b"%PDF-1.4" + b"w" * 200)
    return d


@pytest.fixture
def fake(media_dir: Path):
    server = FakeChatwoot(media_dir=media_dir)
    base = server.start()
    server.base_url = base
    yield server
    server.stop()


@pytest.fixture
def data_dir(tmp_path: Path, fake: FakeChatwoot) -> Path:
    """Two contacts, overlapping exports, media of every kind, duplicates and an unsupported message."""
    d = tmp_path / "data"
    d.mkdir()
    m = f"{fake.base_url}/media"
    contacts = [
        {"ContactID": "1001", "FirstName": "Alice", "LastName": "Smith", "PhoneNumber": "971500000001", "Country": "AE", "Lifecycle": "Source a Watch", "DateTimeCreated": "2026-01-01 10:00:00"},
        {"ContactID": "1002", "FirstName": "Bob", "LastName": "", "PhoneNumber": "919800000002", "Country": "IN", "DateTimeCreated": "2026-01-02 10:00:00"},
        {"ContactID": "1003", "FirstName": "NoPhone", "LastName": "", "PhoneNumber": "", "Country": "US", "DateTimeCreated": "2026-01-03 10:00:00"},
    ]
    convs = [
        {"Conversation ID": "c1", "DateTime Conversation Started": "2026-01-10 12:00:00", "DateTime Conversation Resolved": "2026-01-10 13:00:00", "Contact ID": "1001", "Number of Outgoing Messages": "3", "Number of Incoming Messages": "4", "Opened By Channel": "437642"},
        {"Conversation ID": "c2", "DateTime Conversation Started": "2026-02-01 09:00:00", "DateTime Conversation Resolved": "2026-02-01 10:00:00", "Contact ID": "1001", "Number of Outgoing Messages": "1", "Number of Incoming Messages": "1", "Opened By Channel": "437642"},
    ]
    msgs_a = [
        msg("2026-01-10 12:00:05", "1001", "1", "text", "incoming", {"type": "text", "text": "Hey Moe! Can you help me source a watch?"}),
        msg("2026-01-10 12:00:10", "1001", "2", "quick_reply", "outgoing", {"type": "quick_reply", "title": "Dubai only. Works?", "replies": ["Yes", "No"]}),
        msg("2026-01-10 12:00:20", "1001", "3", "text", "incoming", {"type": "text", "text": "Yes"}),
        msg("2026-01-10 12:01:00", "1001", "4", "attachment", "incoming", {"type": "attachment", "attachment": {"type": "image", "url": f"{m}/photo.jpg", "mimeType": "image/jpeg", "fileName": "photo.jpg", "ext": "jpg", "size": "503"}}),
        msg("2026-01-10 12:02:00", "1001", "5", "attachment", "incoming", {"type": "attachment", "attachment": {"type": "audio", "url": f"{m}/voice.ogg", "mimeType": "audio/ogg; codecs=opus", "fileName": "File.ogg", "ext": "opus", "size": "304"}}),
        msg("2026-01-10 12:03:00", "1001", "6", "whatsapp_template", "outgoing", {"type": "whatsapp_template", "template": {"name": "notification", "components": [{"type": "body", "text": "Lead Name: Alice"}, {"type": "buttons", "buttons": [{"type": "quick_reply", "text": "Call me"}]}]}}, sender_type="broadcast"),
        msg("2026-01-10 12:04:00", "1001", "7", "unsupported", "incoming", {"type": "unsupported", "payload": {"timestamp": "1768032240", "errors": []}}),
        # between windows -> attaches to c1
        msg("2026-01-20 12:00:00", "1001", "8", "text", "outgoing", {"type": "text", "text": "Any update?"}, sender_type="user", sender="311273"),
        # c2
        msg("2026-02-01 09:00:10", "1001", "9", "attachment", "incoming", {"type": "attachment", "attachment": {"type": "video", "url": f"{m}/clip.mp4", "mimeType": "video/mp4", "fileName": "clip.mp4", "ext": "mp4", "size": "808"}}),
        msg("2026-02-01 09:05:00", "1001", "10", "attachment", "outgoing", {"type": "attachment", "attachment": {"type": "file", "url": f"{m}/doc.pdf", "mimeType": "application/pdf", "fileName": "invoice.pdf", "ext": "pdf", "size": "208"}}, sender_type="user", sender="311273"),
        # bob has no conversation row -> synthetic
        msg("2026-03-01 08:00:00", "1002", "11", "text", "incoming", {"type": "text", "text": "Hello"}),
        msg("2026-03-01 08:01:00", "1002", "12", "location", "outgoing", {"type": "location", "latitude": 25.2, "longitude": 55.3, "address": "Dubai"}),
        # contact 9999 missing from contacts export
        msg("2026-03-02 08:00:00", "9999", "13", "text", "incoming", {"type": "text", "text": "Who dis"}),
        # same Message ID as Alice's first message but a different contact: must be kept
        msg("2026-01-10 12:00:05", "1003", "1", "text", "incoming", {"type": "text", "text": "Hi from NoPhone"}),
    ]
    msgs_b = [
        msgs_a[0],  # exact duplicate across exports
        msg("2026-03-05 08:00:00", "1002", "14", "attachment", "incoming", {"type": "attachment", "attachment": {"type": "image", "url": f"{m}/missing.jpg", "mimeType": "image/jpeg", "fileName": "missing.jpg", "ext": "jpg", "size": "1"}}),
    ]
    write_csv(d / "contacts.csv", CONTACT_COLUMNS, contacts)
    write_csv(d / "conversations.csv", CONV_COLUMNS, convs)
    write_csv(d / "messages_1.csv", MSG_COLUMNS, msgs_a)
    write_csv(d / "messages_2.csv", MSG_COLUMNS, msgs_b)
    return d


@pytest.fixture
def env_file(tmp_path: Path, fake: FakeChatwoot, data_dir: Path) -> Path:
    p = tmp_path / ".env"
    p.write_text(
        "\n".join(
            [
                f"CHATWOOT_BASE_URL={fake.base_url}",
                f"CHATWOOT_ACCOUNT_ID={fake.account_id}",
                f"CHATWOOT_API_TOKEN={fake.token}",
                "CHATWOOT_ARCHIVE_INBOX_ID=145492",
                "CHATWOOT_ARCHIVE_INBOX_NAME=Respond.io Archive",
                "SOURCE_TIMEZONE=Asia/Dubai",
                f"DATA_DIR={data_dir}",
                f"MEDIA_DIR={tmp_path / 'media'}",
                f"OUTPUT_DIR={tmp_path / 'output'}",
                "REQUESTS_PER_MINUTE=100000",
                "",
            ]
        )
    )
    return p
