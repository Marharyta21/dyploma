"""Turn raw CSV rows into a migration plan.

Responsibilities:
* de-duplicate contacts, conversations and messages (the two exports overlap);
* attach every message to a conversation (the messages export has no
  conversation id, so we use the contact id plus the conversation time window);
* build synthetic conversations for contacts that have messages but no
  conversation row, and placeholder contacts for ids missing from the contacts
  export;
* keep the original chronological order inside each conversation.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

from .csv_loader import RawData
from .models import ContactPlan, Issue, MigrationPlan, SourceContact, SourceConversation, SourceMessage

SUPPORTED_CONTENT_TYPES = {"text", "quick_reply", "whatsapp_template", "attachment", "sticker", "location", "contact"}


def _msg_sort_key(m: SourceMessage):
    try:
        mid = int(m.message_id)
    except ValueError:
        mid = 0
    return (m.timestamp, mid, m.row_number)


def build_plan(raw: RawData, logger: Optional[logging.Logger] = None) -> MigrationPlan:
    log = logger or logging.getLogger("migration")
    issues: list[Issue] = []
    stats: dict = {}

    # ---- contacts -------------------------------------------------------
    contacts: dict[str, SourceContact] = {}
    for c in raw.contacts:
        if c.contact_id in contacts:
            issues.append(Issue("duplicate_contact", "contact", c.contact_id, f"{c.source_file}:{c.row_number}"))
            continue
        contacts[c.contact_id] = c
    stats["contacts_rows"] = len(raw.contacts)
    stats["contacts_unique"] = len(contacts)

    # ---- conversations --------------------------------------------------
    conversations: dict[str, SourceConversation] = {}
    conv_dup_identical = conv_dup_conflict = 0
    for conv in raw.conversations:
        existing = conversations.get(conv.conversation_id)
        if existing is None:
            conversations[conv.conversation_id] = conv
            continue
        same = (
            existing.contact_id == conv.contact_id
            and existing.started_at == conv.started_at
            and existing.resolved_at == conv.resolved_at
        )
        if same:
            conv_dup_identical += 1
        else:
            conv_dup_conflict += 1
            issues.append(
                Issue(
                    "duplicate_conversation_conflict",
                    "conversation",
                    conv.conversation_id,
                    f"kept {existing.source_file}:{existing.row_number}, dropped {conv.source_file}:{conv.row_number}",
                )
            )
    stats["conversations_rows"] = len(raw.conversations)
    stats["conversations_unique"] = len(conversations)
    stats["conversations_duplicate_identical"] = conv_dup_identical
    stats["conversations_duplicate_conflicting"] = conv_dup_conflict

    # ---- messages -------------------------------------------------------
    # Keyed by (contact id, message id): respond.io message ids are second-precision timestamps and
    # the same id can legitimately appear for two different contacts.
    messages: dict[str, SourceMessage] = {}
    msg_dup_identical = msg_dup_conflict = 0
    for m in raw.messages:
        existing = messages.get(m.key)
        if existing is None:
            messages[m.key] = m
            continue
        if existing.raw_content == m.raw_content and existing.timestamp == m.timestamp and existing.direction == m.direction:
            msg_dup_identical += 1
        else:
            msg_dup_conflict += 1
            issues.append(
                Issue(
                    "duplicate_message_conflict",
                    "message",
                    m.message_id,
                    f"kept {existing.source_file}:{existing.row_number}, dropped {m.source_file}:{m.row_number}",
                )
            )
    stats["messages_rows"] = len(raw.messages)
    stats["messages_unique"] = len(messages)
    id_owners: dict[str, set[str]] = defaultdict(set)
    for m in messages.values():
        id_owners[m.message_id].add(m.contact_id)
    stats["message_ids_shared_between_contacts"] = sum(1 for owners in id_owners.values() if len(owners) > 1)
    stats["messages_duplicate_identical"] = msg_dup_identical
    stats["messages_duplicate_conflicting"] = msg_dup_conflict

    # ---- placeholder contacts -------------------------------------------
    referenced = {m.contact_id for m in messages.values()} | {c.contact_id for c in conversations.values()}
    missing = sorted(referenced - set(contacts))
    for cid in missing:
        contacts[cid] = SourceContact(contact_id=cid, placeholder=True)
        issues.append(Issue("contact_missing_from_export", "contact", cid, "placeholder contact will be created"))
    stats["contacts_placeholder"] = len(missing)

    # ---- group conversations per contact --------------------------------
    convs_by_contact: dict[str, list[SourceConversation]] = defaultdict(list)
    for conv in conversations.values():
        convs_by_contact[conv.contact_id].append(conv)
    for lst in convs_by_contact.values():
        lst.sort(key=lambda c: (c.started_at, c.conversation_id))

    # ---- assign messages to conversations --------------------------------
    msgs_by_contact: dict[str, list[SourceMessage]] = defaultdict(list)
    for m in messages.values():
        msgs_by_contact[m.contact_id].append(m)

    placement = Counter()
    synthetic = 0
    for cid, msgs in msgs_by_contact.items():
        msgs.sort(key=_msg_sort_key)
        convs = convs_by_contact.get(cid)
        if not convs:
            synthetic += 1
            conv = SourceConversation(
                conversation_id=f"synthetic-{cid}",
                contact_id=cid,
                started_at=msgs[0].timestamp,
                resolved_at=msgs[-1].timestamp,
                started_local=msgs[0].local_time,
                resolved_local=msgs[-1].local_time,
                synthetic=True,
            )
            conversations[conv.conversation_id] = conv
            convs_by_contact[cid] = [conv]
            convs = [conv]
            issues.append(
                Issue("contact_without_conversation_row", "contact", cid, f"{len(msgs)} messages placed in a synthetic conversation")
            )
        for m in msgs:
            target, how = _place_message(m, convs)
            placement[how] += 1
            m.conversation_id = target.conversation_id
            target.messages.append(m)

    for conv in conversations.values():
        conv.messages.sort(key=_msg_sort_key)

    stats["conversations_synthetic"] = synthetic
    stats["message_placement"] = dict(placement)
    stats["conversations_without_messages"] = sum(1 for c in conversations.values() if not c.messages)

    # ---- content types / media -------------------------------------------
    ctype = Counter(m.content_type for m in messages.values())
    stats["content_types"] = dict(ctype)
    stats["unsupported_content_types"] = {k: v for k, v in ctype.items() if k not in SUPPORTED_CONTENT_TYPES}
    stats["messages_incoming"] = sum(1 for m in messages.values() if m.is_incoming)
    stats["messages_outgoing"] = sum(1 for m in messages.values() if not m.is_incoming)
    media_kinds = Counter(m.media.kind for m in messages.values() if m.media)
    stats["media_total"] = sum(media_kinds.values())
    stats["media_by_kind"] = dict(media_kinds)
    stats["media_by_mime"] = dict(Counter(m.media.mime for m in messages.values() if m.media))
    stats["media_hosts"] = dict(Counter(m.media.url.split("/")[2] for m in messages.values() if m.media and "://" in m.media.url))
    stats["sender_types"] = dict(Counter(m.sender_type for m in messages.values()))
    stats["channel_ids"] = dict(Counter(m.channel_id for m in messages.values()))
    stats["contacts_without_phone"] = sum(1 for c in contacts.values() if not c.placeholder and not c.phone_e164)
    stats["contacts_with_email"] = sum(1 for c in contacts.values() if c.email)
    for c in contacts.values():
        if c.phone_raw and not c.phone_e164:
            issues.append(Issue("invalid_phone", "contact", c.contact_id, f"phone {c.phone_raw!r} is not valid E.164"))
    if messages:
        stats["messages_date_range"] = (
            min(m.timestamp for m in messages.values()).isoformat(),
            max(m.timestamp for m in messages.values()).isoformat(),
        )

    # ---- final plan ---------------------------------------------------------
    plans: dict[str, ContactPlan] = {}
    # Deterministic order: contacts with messages first (by earliest message), then the rest.
    order = sorted(
        contacts.values(),
        key=lambda c: (
            0 if msgs_by_contact.get(c.contact_id) else 1,
            msgs_by_contact[c.contact_id][0].timestamp if msgs_by_contact.get(c.contact_id) else datetime.max.replace(tzinfo=timezone.utc),
            c.contact_id,
        ),
    )
    for c in order:
        plans[c.contact_id] = ContactPlan(contact=c, conversations=convs_by_contact.get(c.contact_id, []))
    stats["contacts_with_messages"] = sum(1 for p in plans.values() if p.message_count)
    stats["contacts_without_messages"] = sum(1 for p in plans.values() if not p.message_count)
    stats["conversations_total"] = len(conversations)
    stats["parse_errors"] = len(raw.parse_errors)
    for fname, line, err in raw.parse_errors:
        issues.append(Issue("parse_error", "row", f"{fname}:{line}", err))

    log.info(
        "Plan: %d contacts (%d with messages), %d conversations (%d synthetic), %d messages, %d media",
        len(plans),
        stats["contacts_with_messages"],
        len(conversations),
        synthetic,
        len(messages),
        stats["media_total"],
    )
    return MigrationPlan(contacts=plans, issues=issues, stats=stats)


def _place_message(m: SourceMessage, convs: list[SourceConversation]) -> tuple[SourceConversation, str]:
    """Pick the conversation for a message based on its timestamp."""
    ts = m.timestamp
    for conv in convs:
        end = conv.resolved_at or datetime.max.replace(tzinfo=timezone.utc)
        if conv.started_at <= ts <= end:
            return conv, "in_window"
    if ts < convs[0].started_at:
        return convs[0], "before_first_window"
    # Latest conversation that started before the message (covers gaps between windows and after the last one).
    best = convs[0]
    for conv in convs:
        if conv.started_at <= ts:
            best = conv
    how = "after_last_window" if best is convs[-1] and (best.resolved_at is None or ts > best.resolved_at) else "between_windows"
    return best, how


def select_test_contact(plan: MigrationPlan, preferred_id: Optional[str] = None) -> ContactPlan:
    """Pick one contact with one conversation of roughly 10 messages that has
    incoming, outgoing and at least one media message."""
    if preferred_id:
        if preferred_id not in plan.contacts:
            raise KeyError(f"contact {preferred_id} not found in the exports")
        return plan.contacts[preferred_id]

    def score(p: ContactPlan) -> tuple:
        if p.contact.placeholder or len(p.conversations) != 1 or not p.message_count:
            return (0,)
        conv = p.conversations[0]
        has_in = any(m.is_incoming for m in conv.messages)
        has_out = any(not m.is_incoming for m in conv.messages)
        media = sum(1 for m in conv.messages if m.media and m.media.kind in {"image", "video", "audio", "file"})
        n = len(conv.messages)
        closeness = -abs(n - 10)
        return (1, int(has_in and has_out), min(media, 1), int(8 <= n <= 14), closeness, int(not conv.synthetic))

    best = max(plan.contacts.values(), key=score)
    if score(best)[0] == 0:
        raise ValueError("no suitable test contact found")
    return best
