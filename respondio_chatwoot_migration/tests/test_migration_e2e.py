"""End-to-end tests against the in-memory fake Chatwoot."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import pytest

import migrate
from migration.chatwoot import ChatwootClient, WriteBlocked
from migration.config import load_config
from migration.safety import SafetyError, assert_archive_inbox, run_connection_checks

LOG = logging.getLogger("test")


def run(env_file: Path, *args: str) -> int:
    return migrate.main([*args, "--env-file", str(env_file)])


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_check_passes_and_is_read_only(env_file, fake):
    assert run(env_file, "--check") == 0
    assert all(m == "GET" for m, _ in fake.requests)


def test_check_fails_closed_on_whatsapp_inbox(env_file, fake, tmp_path):
    env = env_file.read_text().replace("CHATWOOT_ARCHIVE_INBOX_ID=145492", "CHATWOOT_ARCHIVE_INBOX_ID=100001")
    bad = tmp_path / "bad.env"
    bad.write_text(env)
    assert run(bad, "--check") == 1
    # even a direct --test refuses before writing anything
    assert run(bad, "--test") == 1
    assert not fake.contacts and not fake.conversations and not fake.messages


def test_check_fails_on_wrong_name_or_locked_inbox(env_file, fake, tmp_path):
    cfg = load_config(env_file)
    client = ChatwootClient(fake.base_url, fake.account_id, fake.token, allow_writes=False)
    fake.inboxes[145492]["name"] = "Something else"
    assert not run_connection_checks(client, cfg, LOG).ok
    fake.inboxes[145492]["name"] = "Respond.io Archive"
    fake.inboxes[145492]["lock_to_single_conversation"] = True
    assert not run_connection_checks(client, cfg, LOG).ok
    with pytest.raises(SafetyError):
        assert_archive_inbox(cfg, fake.inboxes[100001])


def test_dry_run_never_writes(env_file, fake):
    assert run(env_file, "--dry-run", "--full") == 0
    assert all(m == "GET" for m, _ in fake.requests)
    client = ChatwootClient(fake.base_url, fake.account_id, fake.token, allow_writes=False)
    with pytest.raises(WriteBlocked):
        client.create_contact({"inbox_id": 145492, "name": "x"})


def test_test_migration_then_idempotent_rerun(env_file, fake, tmp_path):
    rc = run(env_file, "--test", "--test-contact", "1001")
    assert rc == 0
    # one contact, two conversations, 10 messages, 4 attachments
    assert len(fake.contacts) == 1
    contact = next(iter(fake.contacts.values()))
    assert contact["identifier"] == "respondio:1001"
    assert contact["phone_number"] == "+971500000001"
    assert contact["custom_attributes"]["respondio_contact_id"] == "1001"
    assert len(fake.conversations) == 2
    assert all(c["inbox_id"] == 145492 for c in fake.conversations.values())
    assert all(c["status"] == "resolved" for c in fake.conversations.values())
    msgs = sorted(fake.messages.values(), key=lambda m: m["id"])
    assert len(msgs) == 10
    assert [m["source_id"] for m in msgs] == [f"respondio:1001:{i}" for i in range(1, 11)]
    assert msgs[0]["message_type"] == 0 and msgs[1]["message_type"] == 1
    assert msgs[0]["content_attributes"]["external_created_at"] == "2026-01-10T08:00:05Z"
    assert msgs[0]["content_attributes"]["respondio"]["message_id"] == "1"
    with_att = [m for m in msgs if m["attachments"]]
    assert len(with_att) == 4
    assert {a["file_type"] for m in with_att for a in m["attachments"]} == {"image", "audio", "video", "application"}
    assert any(d["attribute_key"] == "respondio_contact_id" for d in fake.custom_attribute_definitions)

    out = tmp_path / "output"
    summary = read_csv(out / "migration_summary.csv")
    assert summary[0]["imported_messages"] == "10" and summary[0]["imported_media"] == "4"
    validation = read_csv(out / "validation_report.csv")
    assert validation[0]["chatwoot_live_messages"] == "8;2" and validation[0]["chatwoot_live_status"] == "resolved;resolved"
    assert read_csv(out / "failed_messages.csv") == [] and read_csv(out / "failed_media.csv") == []
    assert (out / "migration.log").exists() and (out / "migration_state.db").exists()

    # second run: nothing new is created
    before = (len(fake.contacts), len(fake.conversations), len(fake.messages))
    writes_before = sum(1 for m, _ in fake.requests if m != "GET")
    assert run(env_file, "--test", "--test-contact", "1001") == 0
    assert (len(fake.contacts), len(fake.conversations), len(fake.messages)) == before
    assert sum(1 for m, _ in fake.requests if m != "GET") == writes_before


def test_state_recovery_without_local_db(env_file, fake, tmp_path):
    assert run(env_file, "--test", "--test-contact", "1001") == 0
    for f in (tmp_path / "output").glob("migration_state.db*"):
        f.unlink()
    before = (len(fake.contacts), len(fake.conversations), len(fake.messages))
    assert run(env_file, "--test", "--test-contact", "1001") == 0
    assert (len(fake.contacts), len(fake.conversations), len(fake.messages)) == before


def test_existing_whatsapp_contact_is_reused(env_file, fake):
    # a contact already created by the live WhatsApp inbox with the same phone number
    fake.contacts[42] = {"id": 42, "name": "Alice (WA)", "email": None, "phone_number": "+971500000001", "identifier": None, "custom_attributes": {}, "additional_attributes": {}}
    fake.contact_inboxes.append({"contact_id": 42, "inbox_id": 100001, "source_id": "971500000001"})
    assert run(env_file, "--test", "--test-contact", "1001") == 0
    assert len(fake.contacts) == 1
    assert fake.contacts[42]["identifier"] == "respondio:1001"
    assert fake.contacts[42]["custom_attributes"]["respondio_contact_id"] == "1001"
    archive_ci = [ci for ci in fake.contact_inboxes if ci["contact_id"] == 42 and ci["inbox_id"] == 145492]
    assert len(archive_ci) == 1 and archive_ci[0]["source_id"] == "respondio-1001"
    assert all(c["inbox_id"] == 145492 and c["contact_id"] == 42 for c in fake.conversations.values())


def test_full_requires_confirmation_and_handles_failed_media(env_file, fake, tmp_path):
    assert run(env_file, "--full") == 2
    assert not fake.contacts
    rc = run(env_file, "--full", "--confirm-full-migration")
    assert rc == 1  # bob's conversation stops at the missing media
    out = tmp_path / "output"
    failed_media = read_csv(out / "failed_media.csv")
    assert len(failed_media) == 1 and failed_media[0]["http_status"] == "404"
    failed_msgs = read_csv(out / "failed_messages.csv")
    assert [m["respond_message_id"] for m in failed_msgs] == ["14"]
    assert len(fake.contacts) == 4  # alice, bob, NoPhone (identifier only), placeholder 9999
    bob_conv = [c for c in fake.conversations.values() if c["additional_attributes"]["respondio_contact_id"] == "1002"]
    assert len(bob_conv) == 1 and bob_conv[0]["status"] == "open"  # not resolved: conversation incomplete
    # re-run with placeholders: the missing media becomes a text note and the conversation gets resolved
    rc = run(env_file, "--full", "--confirm-full-migration", "--placeholder-for-failed-media")
    assert rc == 0
    bob_conv = [c for c in fake.conversations.values() if c["additional_attributes"]["respondio_contact_id"] == "1002"]
    assert bob_conv[0]["status"] == "resolved"
    placeholder = [m for m in fake.messages.values() if m["source_id"] == "respondio:1002:14"]
    assert len(placeholder) == 1 and "Media not available" in placeholder[0]["content"]
    assert read_csv(out / "failed_messages.csv") == []
    assert len(fake.messages) == 15
    nophone = next(c for c in fake.contacts.values() if c["identifier"] == "respondio:1003")
    assert nophone["phone_number"] is None


def test_flood_limit_is_retried(env_file, fake, monkeypatch):
    fake.flood_limit = 3
    monkeypatch.setattr("migration.migrator.time.sleep", lambda s: None)
    import migration.migrator as mm

    orig = mm.Migrator.__init__

    def patched(self, *a, **kw):
        kw["sleep_fn"] = lambda s: None
        orig(self, *a, **kw)

    monkeypatch.setattr(mm.Migrator, "__init__", patched)
    assert run(env_file, "--test", "--test-contact", "1001") == 0
    assert len(fake.messages) == 10


def test_media_test_stage(env_file, fake, tmp_path):
    rc = run(env_file, "--media-test")
    rows = read_csv(tmp_path / "output" / "media_test_report.csv")
    kinds = {r["kind"] for r in rows if r["status"] == "success"}
    assert {"image", "audio", "video", "file"} <= kinds
    assert any(r["status"] == "failed" for r in rows)  # missing.jpg
    assert rc == 1
    assert (tmp_path / "media").exists() and any((tmp_path / "media").iterdir())
    assert not fake.contacts  # no Chatwoot writes in stage B
