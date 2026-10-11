from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from migration.assembler import build_plan, select_test_contact
from migration.csv_loader import CsvSchemaError, detect_kind, load_data_dir, parse_local_datetime
from migration.renderer import UNSUPPORTED_TEXT, render

TZ = ZoneInfo("Asia/Dubai")
LOG = logging.getLogger("test")


def test_detect_kind_by_header():
    assert detect_kind(["ContactID", "FirstName", "LastName", "PhoneNumber", "Email"]) == "contacts"
    assert detect_kind(["Conversation ID", "DateTime Conversation Started", "DateTime Conversation Resolved", "Contact ID"]) == "conversations"
    assert detect_kind(["Date & Time", "Sender ID", "Sender Type", "Contact ID", "Message ID", "Content Type", "Message Type", "Content", "Channel ID"]) == "messages"
    assert detect_kind(["foo", "bar"]) is None


def test_local_datetime_converted_to_utc():
    dt = parse_local_datetime("2026-01-11 22:11:23", TZ)
    assert dt == datetime(2026, 1, 11, 18, 11, 23, tzinfo=timezone.utc)
    assert parse_local_datetime("", TZ) is None
    with pytest.raises(ValueError):
        parse_local_datetime("yesterday", TZ)


def test_missing_data_dir(tmp_path):
    with pytest.raises(CsvSchemaError):
        load_data_dir(tmp_path / "nope", TZ, LOG)


def test_plan_dedupes_links_and_synthesises(data_dir):
    raw = load_data_dir(data_dir, TZ, LOG)
    plan = build_plan(raw, LOG)
    s = plan.stats
    assert s["messages_rows"] == 16 and s["messages_unique"] == 15
    assert s["messages_duplicate_identical"] == 1 and s["messages_duplicate_conflicting"] == 0
    assert s["message_ids_shared_between_contacts"] == 1  # id "1" used by Alice and NoPhone
    assert s["contacts_placeholder"] == 1  # contact 9999
    assert s["conversations_synthetic"] == 3  # bob + 9999 + NoPhone
    assert plan.contacts["1003"].message_count == 1
    alice = plan.contacts["1001"]
    assert [c.conversation_id for c in alice.conversations] == ["c1", "c2"]
    c1, c2 = alice.conversations
    assert [m.message_id for m in c1.messages] == ["1", "2", "3", "4", "5", "6", "7", "8"]  # msg 8 sits between windows -> c1
    assert [m.message_id for m in c2.messages] == ["9", "10"]
    assert alice.media_count == 4
    assert s["message_placement"]["between_windows"] == 1
    bob = plan.contacts["1002"]
    assert bob.conversations[0].synthetic and bob.conversations[0].conversation_id == "synthetic-1002"
    assert plan.contacts["9999"].contact.placeholder
    assert plan.contacts["1003"].contact.phone_e164 is None
    assert alice.contact.phone_e164 == "+971500000001"
    assert {i.kind for i in plan.issues} >= {"contact_missing_from_export", "contact_without_conversation_row"}


def test_select_test_contact_prefers_mixed_conversation(data_dir):
    plan = build_plan(load_data_dir(data_dir, TZ, LOG), LOG)
    chosen = select_test_contact(plan)
    assert chosen.contact.contact_id == "1002" or chosen.contact.contact_id == "1001"
    assert select_test_contact(plan, "1001").contact.contact_id == "1001"
    with pytest.raises(KeyError):
        select_test_contact(plan, "does-not-exist")


def test_renderer_covers_every_payload_type(data_dir):
    plan = build_plan(load_data_dir(data_dir, TZ, LOG), LOG)
    by_id = {m.key: m for _, _, m in plan.iter_messages()}
    assert render(by_id["1001:1"]).content.startswith("Hey Moe")
    assert render(by_id["1003:1"]).content == "Hi from NoPhone"
    qr = render(by_id["1001:2"]).content
    assert "Dubai only. Works?" in qr and "[Quick replies: Yes | No]" in qr
    tpl = render(by_id["1001:6"])
    assert "Lead Name: Alice" in tpl.content and "[Buttons: Call me]" in tpl.content and tpl.template_name == "notification"
    assert render(by_id["1001:7"]).content == UNSUPPORTED_TEXT and render(by_id["1001:7"]).unsupported
    loc = render(by_id["1002:12"]).content
    assert "Dubai" in loc and "maps.google.com/?q=25.2,55.3" in loc
    att = render(by_id["1001:4"])
    assert att.content == "" and att.media and att.media.kind == "image"
    stamped = render(by_id["1001:1"], timestamp_prefix=True, tz=TZ).content
    assert stamped.startswith("🕓 2026-01-10 12:00")
