"""The migration engine (Stage D test run and Stage F full run).

Flow per respond.io contact:
  contact -> Chatwoot contact -> contact inbox (source) in the archive API inbox
  -> conversation -> messages (chronological, incoming/outgoing, attachments)
  -> conversation marked resolved.

Everything is idempotent: mappings are stored in SQLite before moving on, and
when a mapping is missing the engine looks for the object in Chatwoot (by
identifier / phone / additional_attributes / message source_id) before creating
anything.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import timezone
from typing import Optional

from .chatwoot import ChatwootClient, ChatwootHTTPError, WriteBlocked
from .config import Config
from .media import MediaDownloader
from .models import ContactPlan, SourceContact, SourceConversation, SourceMessage
from .renderer import placeholder_for_missing_media, render
from .safety import CONTACT_ATTRIBUTE_KEY, SafetyError, assert_archive_inbox, assert_conversation_in_archive
from .state import StateDB

TOO_MANY_MESSAGES_RETRIES = 4


class ConversationAborted(Exception):
    """A message could not be imported; stop this conversation to keep the order intact."""


@dataclass
class RunStats:
    contacts_seen: int = 0
    contacts_created: int = 0
    contacts_reused: int = 0
    contacts_failed: int = 0
    conversations_created: int = 0
    conversations_reused: int = 0
    conversations_resolved: int = 0
    conversations_failed: int = 0
    messages_created: int = 0
    messages_skipped_existing: int = 0
    messages_failed: int = 0
    media_uploaded: int = 0
    media_failed: int = 0
    placeholders_for_media: int = 0
    would_create: dict = field(default_factory=lambda: defaultdict(int))

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "would_create"}
        d.update({f"would_create_{k}": v for k, v in self.would_create.items()})
        return d


class Migrator:
    def __init__(
        self,
        cfg: Config,
        client: ChatwootClient,
        state: StateDB,
        downloader: MediaDownloader,
        dry_run: bool,
        logger: Optional[logging.Logger] = None,
        placeholder_for_failed_media: bool = False,
        sleep_fn=time.sleep,
    ):
        self.cfg = cfg
        self.client = client
        self.state = state
        self.downloader = downloader
        self.dry_run = dry_run
        self.log = logger or logging.getLogger("migration")
        self.placeholder_for_failed_media = placeholder_for_failed_media
        self.sleep = sleep_fn
        self.stats = RunStats()
        self._conv_windows: dict[int, deque[float]] = defaultdict(deque)
        self._archive_verified = False
        self._custom_attribute_ensured = False

    # ------------------------------------------------------------ guards
    def verify_archive_inbox(self) -> dict:
        inbox = self.client.get_inbox(self.cfg.archive_inbox_id)
        assert_archive_inbox(self.cfg, inbox)
        if inbox.get("lock_to_single_conversation"):
            raise SafetyError("archive inbox has lock_to_single_conversation enabled; refusing to import")
        self._archive_verified = True
        return inbox

    def ensure_custom_attribute_definition(self) -> None:
        if self._custom_attribute_ensured or self.dry_run:
            return
        defs = self.client.list_custom_attribute_definitions("contact_attribute")
        if not any(d.get("attribute_key") == CONTACT_ATTRIBUTE_KEY for d in defs):
            self.log.info("Creating contact custom attribute definition %r", CONTACT_ATTRIBUTE_KEY)
            self.client.create_custom_attribute_definition(
                {
                    "attribute_display_name": "respond.io contact ID",
                    "attribute_display_type": 0,  # text
                    "attribute_description": "Contact ID in the former respond.io workspace",
                    "attribute_key": CONTACT_ATTRIBUTE_KEY,
                    "attribute_model": 1,  # contact_attribute
                }
            )
        self._custom_attribute_ensured = True

    # ------------------------------------------------------------ entry
    def migrate_contact_plan(self, plan: ContactPlan) -> None:
        if not self.dry_run and not self._archive_verified:
            self.verify_archive_inbox()
        self.stats.contacts_seen += 1
        contact = plan.contact
        if self.dry_run:
            self._dry_run_plan(plan)
            return
        try:
            chatwoot_contact_id = self.ensure_contact(contact)
            source_id = self.ensure_contact_inbox(contact, chatwoot_contact_id)
        except (ChatwootHTTPError, SafetyError, WriteBlocked) as exc:
            self.stats.contacts_failed += 1
            self.log.error("Contact %s failed: %s", contact.contact_id, exc)
            self.state.save_contact(contact.contact_id, None, None, None, "failed", str(exc))
            return

        for conv in plan.conversations:
            if not conv.messages:
                self.log.info("Conversation %s has no messages in the export; nothing to import", conv.conversation_id)
                continue
            try:
                self.migrate_conversation(contact, conv, chatwoot_contact_id, source_id)
            except (ChatwootHTTPError, SafetyError, WriteBlocked, ConversationAborted) as exc:
                self.stats.conversations_failed += 1
                self.log.error("Conversation %s (contact %s) stopped: %s", conv.conversation_id, contact.contact_id, exc)
                rec = self.state.get_conversation(conv.conversation_id)
                self.state.save_conversation(
                    conv.conversation_id,
                    contact.contact_id,
                    rec.chatwoot_conversation_id if rec else None,
                    "failed",
                    str(exc),
                )

    def _dry_run_plan(self, plan: ContactPlan) -> None:
        w = self.stats.would_create
        rec = self.state.get_contact(plan.contact.contact_id)
        if rec and rec.chatwoot_contact_id:
            w["contacts_already_mapped"] += 1
        else:
            w["contacts"] += 1
        for conv in plan.conversations:
            if not conv.messages:
                continue
            crec = self.state.get_conversation(conv.conversation_id)
            if crec and crec.chatwoot_conversation_id:
                w["conversations_already_mapped"] += 1
            else:
                w["conversations"] += 1
            done = self.state.messages_for_conversation(conv.conversation_id)
            for m in conv.messages:
                if m.key in done and done[m.key].status == "done":
                    w["messages_already_mapped"] += 1
                else:
                    w["messages"] += 1
                    if m.media:
                        w["media_downloads"] += 1
            w["conversations_to_resolve"] += 1

    # ------------------------------------------------------------ contacts
    def _contact_payload(self, contact: SourceContact) -> dict:
        additional = {
            "respondio": {
                "contact_id": contact.contact_id,
                "status": contact.status,
                "lifecycle": contact.lifecycle,
                "tags": contact.tags,
                "assignee": contact.assignee,
                "channels": contact.channels,
                "country": contact.country,
                "language": contact.language,
                "created_at": contact.created_at.isoformat() if contact.created_at else "",
                "last_interaction_at": contact.last_interaction_at.isoformat() if contact.last_interaction_at else "",
                **{k: v for k, v in contact.extra.items()},
            }
        }
        if contact.country:
            additional["country_code"] = contact.country
        payload = {
            "inbox_id": self.cfg.archive_inbox_id,
            "name": contact.name[:255],
            "identifier": contact.identifier,
            "custom_attributes": {CONTACT_ATTRIBUTE_KEY: contact.contact_id},
            "additional_attributes": additional,
        }
        if contact.phone_e164:
            payload["phone_number"] = contact.phone_e164
        if contact.email:
            payload["email"] = contact.email
        return payload

    def _find_existing_contact(self, contact: SourceContact) -> Optional[dict]:
        """Look for the contact in Chatwoot by identifier, then phone, then email."""
        for c in self.client.search_contacts(contact.identifier):
            if c.get("identifier") == contact.identifier:
                return c
        if contact.phone_e164:
            for c in self.client.search_contacts(contact.phone_e164):
                if c.get("phone_number") == contact.phone_e164:
                    return c
        if contact.email:
            for c in self.client.search_contacts(contact.email):
                if (c.get("email") or "").lower() == contact.email:
                    return c
        return None

    def ensure_contact(self, contact: SourceContact) -> Optional[int]:
        rec = self.state.get_contact(contact.contact_id)
        if rec and rec.chatwoot_contact_id:
            return rec.chatwoot_contact_id
        if self.dry_run:
            return None
        self.ensure_custom_attribute_definition()

        existing = self._find_existing_contact(contact)
        if existing:
            cid = int(existing["id"])
            updates: dict = {}
            if not existing.get("identifier"):
                updates["identifier"] = contact.identifier
            ca = existing.get("custom_attributes") or {}
            if str(ca.get(CONTACT_ATTRIBUTE_KEY, "")) != contact.contact_id:
                updates["custom_attributes"] = {**ca, CONTACT_ATTRIBUTE_KEY: contact.contact_id}
            if updates:
                self.client.update_contact(cid, updates)
            self.log.info("Contact %s -> reusing existing Chatwoot contact %d (%s)", contact.contact_id, cid, existing.get("name"))
            self.stats.contacts_reused += 1
            self.state.save_contact(contact.contact_id, cid, None, "reused", "done")
            return cid

        payload = self._contact_payload(contact)
        try:
            created = self.client.create_contact(payload)
        except ChatwootHTTPError as exc:
            if exc.status == 422:
                # Race / soft duplicate (phone, email or identifier already taken): fall back to lookup.
                self.log.warning("Create contact %s returned 422 (%s); retrying lookup", contact.contact_id, exc.body[:200])
                existing = self._find_existing_contact(contact)
                if existing:
                    cid = int(existing["id"])
                    self.stats.contacts_reused += 1
                    self.state.save_contact(contact.contact_id, cid, None, "reused", "done")
                    return cid
                if "phone" in exc.body.lower() and "phone_number" in payload:
                    # Invalid for Chatwoot even though it looked like E.164: create without the phone.
                    payload.pop("phone_number")
                    created = self.client.create_contact(payload)
                else:
                    raise
            else:
                raise
        contact_json = created["contact"]
        cid = int(contact_json["id"])
        source_id = None
        ci = created.get("contact_inbox") or {}
        if ci and int((ci.get("inbox") or {}).get("id", self.cfg.archive_inbox_id)) == self.cfg.archive_inbox_id:
            source_id = ci.get("source_id")
        self.log.info("Contact %s -> created Chatwoot contact %d", contact.contact_id, cid)
        self.stats.contacts_created += 1
        self.state.save_contact(contact.contact_id, cid, source_id, "created", "done")
        return cid

    def ensure_contact_inbox(self, contact: SourceContact, chatwoot_contact_id: int) -> str:
        rec = self.state.get_contact(contact.contact_id)
        if rec and rec.source_id:
            return rec.source_id
        detail = self.client.get_contact(chatwoot_contact_id)
        for ci in detail.get("contact_inboxes", []) or []:
            inbox = ci.get("inbox") or {}
            if int(inbox.get("id", -1)) == self.cfg.archive_inbox_id and ci.get("source_id"):
                self.state.save_contact(contact.contact_id, chatwoot_contact_id, ci["source_id"], None, "done")
                return ci["source_id"]
        source_id = f"respondio-{contact.contact_id}"
        resp = self.client.create_contact_inbox(chatwoot_contact_id, self.cfg.archive_inbox_id, source_id)
        source_id = (resp or {}).get("source_id") or source_id
        self.state.save_contact(contact.contact_id, chatwoot_contact_id, source_id, None, "done")
        return source_id

    # ------------------------------------------------------------ conversations
    def _conversation_payload(self, contact: SourceContact, conv: SourceConversation, chatwoot_contact_id: int, source_id: str) -> dict:
        return {
            "inbox_id": self.cfg.archive_inbox_id,
            "contact_id": chatwoot_contact_id,
            "source_id": source_id,
            "status": "open",
            "additional_attributes": {
                "migrated_from": "respond.io",
                "respondio_conversation_id": conv.conversation_id,
                "respondio_contact_id": contact.contact_id,
                "respondio_started_at": conv.started_at.astimezone(timezone.utc).isoformat(),
                "respondio_resolved_at": conv.resolved_at.astimezone(timezone.utc).isoformat() if conv.resolved_at else "",
                "respondio_channel_id": conv.opened_by_channel,
                "respondio_assignee": conv.assignee,
                "respondio_category": conv.category,
                "respondio_closing_note": conv.closing_note,
                "respondio_synthetic": conv.synthetic,
            },
        }

    def _find_existing_conversation(self, conv: SourceConversation, chatwoot_contact_id: int) -> Optional[dict]:
        for c in self.client.list_contact_conversations(chatwoot_contact_id):
            attrs = c.get("additional_attributes") or {}
            if str(attrs.get("respondio_conversation_id", "")) == conv.conversation_id:
                return c
        return None

    def migrate_conversation(self, contact: SourceContact, conv: SourceConversation, chatwoot_contact_id: int, source_id: str) -> None:
        rec = self.state.get_conversation(conv.conversation_id)
        if rec and rec.status == "resolved" and rec.chatwoot_conversation_id:
            done = self.state.messages_for_conversation(conv.conversation_id)
            if all(m.key in done and done[m.key].status == "done" for m in conv.messages):
                self.stats.messages_skipped_existing += len(conv.messages)
                self.log.info("Conversation %s already migrated (Chatwoot %d); skipping", conv.conversation_id, rec.chatwoot_conversation_id)
                return

        pre_existing = True
        if rec and rec.chatwoot_conversation_id:
            cw_id = rec.chatwoot_conversation_id
            self.stats.conversations_reused += 1
        else:
            existing = self._find_existing_conversation(conv, chatwoot_contact_id)
            if existing:
                assert_conversation_in_archive(self.cfg, existing)
                cw_id = int(existing["id"])
                self.stats.conversations_reused += 1
                self.log.info("Conversation %s -> reusing Chatwoot conversation %d", conv.conversation_id, cw_id)
            else:
                created = self.client.create_conversation(self._conversation_payload(contact, conv, chatwoot_contact_id, source_id))
                assert_conversation_in_archive(self.cfg, created)
                cw_id = int(created["id"])
                pre_existing = False
                self.stats.conversations_created += 1
                self.log.info("Conversation %s -> created Chatwoot conversation %d", conv.conversation_id, cw_id)
            self.state.save_conversation(conv.conversation_id, contact.contact_id, cw_id, "created")

        self.migrate_messages(contact, conv, cw_id, recover=pre_existing)
        self.state.save_conversation(conv.conversation_id, contact.contact_id, cw_id, "messages_done")

        self.resolve_conversation(conv, cw_id)
        self.state.save_conversation(conv.conversation_id, contact.contact_id, cw_id, "resolved")

    def resolve_conversation(self, conv: SourceConversation, cw_id: int) -> None:
        current = self.client.get_conversation(cw_id)
        assert_conversation_in_archive(self.cfg, current)
        if current.get("status") == "resolved":
            return
        resp = self.client.toggle_status(cw_id, "resolved")
        payload = (resp or {}).get("payload", resp) or {}
        status = payload.get("current_status") or payload.get("status")
        if status and status != "resolved":
            raise ChatwootHTTPError("POST", f"toggle_status {cw_id}", 200, f"status is {status!r} after toggle")
        self.stats.conversations_resolved += 1
        self.log.info("Conversation %s (Chatwoot %d) marked resolved", conv.conversation_id, cw_id)

    # ------------------------------------------------------------ messages
    def _recover_message_mappings(self, conv: SourceConversation, cw_id: int) -> None:
        """Rebuild message mappings from Chatwoot when the local state is incomplete."""
        done = self.state.messages_for_conversation(conv.conversation_id)
        if all(m.key in done and done[m.key].status == "done" for m in conv.messages):
            return
        existing = self.client.list_all_messages(cw_id)
        by_source = {}
        for m in existing:
            sid = m.get("source_id") or ""
            if sid.startswith("respondio:"):
                by_source[sid[len("respondio:"):]] = m
            ca = m.get("content_attributes") or {}
            rio = ca.get("respondio") or {}
            if rio.get("message_id"):
                by_source[f"{rio.get('contact_id', conv.contact_id)}:{rio['message_id']}"] = m
        recovered = 0
        for m in conv.messages:
            if m.key in done and done[m.key].status == "done":
                continue
            hit = by_source.get(m.key)
            if hit:
                self.state.save_message(
                    m.key,
                    m.message_id,
                    conv.conversation_id,
                    conv.contact_id,
                    int(hit["id"]),
                    cw_id,
                    m.media is not None,
                    bool(hit.get("attachments")),
                    "done",
                )
                recovered += 1
        if recovered:
            self.log.info("Recovered %d message mappings for conversation %s from Chatwoot", recovered, conv.conversation_id)

    def _throttle_conversation(self, cw_id: int) -> None:
        window = self._conv_windows[cw_id]
        now = time.monotonic()
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.cfg.conversation_messages_per_minute:
            wait = 60 - (now - window[0]) + 0.5
            self.log.info("Per-conversation limit reached for %d; sleeping %.1fs", cw_id, wait)
            self.sleep(max(wait, 0.1))
            window.clear()
        window.append(time.monotonic())

    def _message_attributes(self, msg: SourceMessage, rendered) -> dict:
        attrs = {
            "respondio": {
                "message_id": msg.message_id,
                "contact_id": msg.contact_id,
                "conversation_id": msg.conversation_id,
                "sender_type": msg.sender_type,
                "sender_id": msg.sender_id,
                "content_type": msg.content_type,
                "channel_id": msg.channel_id,
                "sent_at_utc": msg.timestamp.astimezone(timezone.utc).isoformat(),
                "sent_at_local": msg.local_time,
                "source_timezone": self.cfg.source_timezone,
            }
        }
        if rendered.template_name:
            attrs["respondio"]["template"] = rendered.template_name
        if msg.sub_type:
            attrs["respondio"]["sub_type"] = msg.sub_type
        if msg.media:
            attrs["respondio"]["media_url"] = msg.media.url
            attrs["respondio"]["media_kind"] = msg.media.kind
        if rendered.unsupported:
            attrs["respondio"]["unsupported"] = True
        return attrs

    def migrate_messages(self, contact: SourceContact, conv: SourceConversation, cw_id: int, recover: bool) -> None:
        if recover:
            self._recover_message_mappings(conv, cw_id)
        done = self.state.messages_for_conversation(conv.conversation_id)
        for msg in conv.messages:
            rec = done.get(msg.key)
            if rec and rec.status == "done" and rec.chatwoot_message_id:
                self.stats.messages_skipped_existing += 1
                continue
            self._import_message(contact, conv, msg, cw_id)

    def _import_message(self, contact: SourceContact, conv: SourceConversation, msg: SourceMessage, cw_id: int) -> None:
        rendered = render(msg, timestamp_prefix=self.cfg.timestamp_prefix, tz=self.cfg.tz)
        attachment_path = None
        attachment_mime = None
        attachment_name = None
        media_uploaded = False
        content = rendered.content

        if msg.media:
            res = self.downloader.download(msg)
            if res.ok and res.path:
                attachment_path = res.path
                attachment_mime = (msg.media.mime or "application/octet-stream").split(";")[0].strip() or "application/octet-stream"
                attachment_name = res.path.name.split("_", 2)[-1]
            else:
                self.stats.media_failed += 1
                if not self.placeholder_for_failed_media:
                    err = f"media download failed: {res.error}"
                    self.stats.messages_failed += 1
                    self.state.save_message(msg.key, msg.message_id, conv.conversation_id, contact.contact_id, None, cw_id, True, False, "failed", err)
                    raise ConversationAborted(f"message {msg.message_id}: {err} (re-run to retry, or use --placeholder-for-failed-media)")
                content = placeholder_for_missing_media(rendered, msg.media, res.error or "download failed")
                self.stats.placeholders_for_media += 1

        attrs = self._message_attributes(msg, rendered)
        external_created_at = msg.timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        source_id = msg.chatwoot_source_id

        attempt = 0
        while True:
            attempt += 1
            self._throttle_conversation(cw_id)
            try:
                resp = self.client.create_message(
                    cw_id,
                    content=content,
                    message_type=msg.direction,
                    content_attributes=attrs,
                    external_created_at=external_created_at,
                    source_id=source_id,
                    attachment_path=attachment_path,
                    attachment_mime=attachment_mime,
                    attachment_name=attachment_name,
                )
                break
            except ChatwootHTTPError as exc:
                if exc.status == 422 and "too many messages" in exc.body.lower() and attempt <= TOO_MANY_MESSAGES_RETRIES:
                    self.log.warning("Chatwoot per-conversation flood limit hit on %d; sleeping 61s", cw_id)
                    self.sleep(61)
                    self._conv_windows[cw_id].clear()
                    continue
                self.stats.messages_failed += 1
                self.state.save_message(
                    msg.key, msg.message_id, conv.conversation_id, contact.contact_id, None, cw_id, msg.media is not None, False, "failed", str(exc)
                )
                raise ConversationAborted(f"message {msg.message_id}: {exc}") from exc

        cw_msg_id = int(resp["id"]) if isinstance(resp, dict) and resp.get("id") is not None else None
        if attachment_path is not None:
            media_uploaded = bool(resp.get("attachments")) if isinstance(resp, dict) else True
            if media_uploaded:
                self.stats.media_uploaded += 1
            else:
                self.log.warning("Message %s created (%s) but Chatwoot reported no attachment", msg.message_id, cw_msg_id)
        self.stats.messages_created += 1
        self.state.save_message(
            msg.key, msg.message_id, conv.conversation_id, contact.contact_id, cw_msg_id, cw_id, msg.media is not None, media_uploaded, "done"
        )
        self.log.debug("Message %s -> Chatwoot %s (%s, %s)", msg.message_id, cw_msg_id, msg.direction, rendered.kind)
