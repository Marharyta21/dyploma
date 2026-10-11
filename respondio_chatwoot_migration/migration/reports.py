"""CSV reports: failed items, migration summary and the Stage E validation report."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Iterable, Optional

from .chatwoot import ChatwootClient, ChatwootHTTPError
from .models import ContactPlan, MigrationPlan
from .state import StateDB


def _write_csv(path: Path, rows: list[dict], fieldnames: Optional[list[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows and not fieldnames:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = fieldnames or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_failed_reports(state: StateDB, out_dir: Path) -> tuple[int, int]:
    msgs = [
        {
            "respond_message_id": m.respond_message_id,
            "respond_conversation_id": m.respond_conversation_id,
            "respond_contact_id": m.respond_contact_id,
            "chatwoot_conversation_id": m.chatwoot_conversation_id or "",
            "has_media": int(m.has_media),
            "error": m.error or "",
        }
        for m in state.failed_messages()
    ]
    _write_csv(
        out_dir / "failed_messages.csv",
        msgs,
        ["respond_message_id", "respond_conversation_id", "respond_contact_id", "chatwoot_conversation_id", "has_media", "error"],
    )
    media = [
        {
            "respond_message_id": m.respond_message_id,
            "url": m.url,
            "kind": m.kind or "",
            "mime": m.mime or "",
            "http_status": m.http_status or "",
            "attempts": m.attempts,
            "error": m.error or "",
        }
        for m in state.failed_media()
    ]
    _write_csv(out_dir / "failed_media.csv", media, ["respond_message_id", "url", "kind", "mime", "http_status", "attempts", "error"])
    return len(msgs), len(media)


def contact_summary_row(plan: ContactPlan, state: StateDB) -> dict:
    crec = state.get_contact(plan.contact.contact_id)
    conv_ids = []
    conv_status = []
    imported = 0
    failed_msgs = 0
    media_imported = 0
    for conv in plan.conversations:
        rec = state.get_conversation(conv.conversation_id)
        if rec and rec.chatwoot_conversation_id:
            conv_ids.append(str(rec.chatwoot_conversation_id))
        conv_status.append(rec.status if rec else "pending")
        msgs = state.messages_for_conversation(conv.conversation_id)
        for m in conv.messages:
            r = msgs.get(m.key)
            if r and r.status == "done":
                imported += 1
                if r.media_uploaded:
                    media_imported += 1
            elif r and r.status == "failed":
                failed_msgs += 1
    return {
        "respond_contact_id": plan.contact.contact_id,
        "contact_name": plan.contact.name,
        "phone": plan.contact.phone_e164 or "",
        "chatwoot_contact_id": crec.chatwoot_contact_id if crec and crec.chatwoot_contact_id else "",
        "contact_action": (crec.action or "") if crec else "",
        "contact_status": crec.status if crec else "pending",
        "source_conversations": len([c for c in plan.conversations if c.messages]),
        "chatwoot_conversation_ids": ";".join(conv_ids),
        "conversation_statuses": ";".join(conv_status),
        "source_messages": plan.message_count,
        "imported_messages": imported,
        "failed_messages": failed_msgs,
        "source_media": plan.media_count,
        "imported_media": media_imported,
        "contact_error": (crec.error or "") if crec else "",
    }


SUMMARY_FIELDS = [
    "respond_contact_id",
    "contact_name",
    "phone",
    "chatwoot_contact_id",
    "contact_action",
    "contact_status",
    "source_conversations",
    "chatwoot_conversation_ids",
    "conversation_statuses",
    "source_messages",
    "imported_messages",
    "failed_messages",
    "source_media",
    "imported_media",
    "contact_error",
]


def write_summary(plans: Iterable[ContactPlan], state: StateDB, out_dir: Path) -> list[dict]:
    rows = [contact_summary_row(p, state) for p in plans]
    _write_csv(out_dir / "migration_summary.csv", rows, SUMMARY_FIELDS)
    return rows


def validation_report(
    plans: list[ContactPlan],
    state: StateDB,
    client: Optional[ChatwootClient],
    out_dir: Path,
    logger: Optional[logging.Logger] = None,
) -> list[dict]:
    """Stage E: compare the export with what the state DB (and Chatwoot, when reachable) says."""
    log = logger or logging.getLogger("migration")
    rows = []
    for plan in plans:
        row = contact_summary_row(plan, state)
        live_messages = ""
        live_attachments = ""
        live_status = ""
        if client is not None:
            counts = []
            atts = []
            statuses = []
            for conv in plan.conversations:
                rec = state.get_conversation(conv.conversation_id)
                if not rec or not rec.chatwoot_conversation_id:
                    continue
                try:
                    msgs = client.list_all_messages(rec.chatwoot_conversation_id)
                    detail = client.get_conversation(rec.chatwoot_conversation_id)
                except ChatwootHTTPError as exc:
                    log.error("validation: cannot read conversation %s: %s", rec.chatwoot_conversation_id, exc)
                    continue
                chat = [m for m in msgs if m.get("message_type") in (0, 1, "incoming", "outgoing") and not m.get("private")]
                counts.append(str(len(chat)))
                atts.append(str(sum(1 for m in chat if m.get("attachments"))))
                statuses.append(str(detail.get("status")))
            live_messages = ";".join(counts)
            live_attachments = ";".join(atts)
            live_status = ";".join(statuses)
        failed_items = []
        for conv in plan.conversations:
            for m in conv.messages:
                r = state.get_message(m.key)
                if r and r.status == "failed":
                    failed_items.append(f"message {m.message_id}: {r.error}")
                mr = state.get_media(m.key)
                if mr and mr.status == "failed":
                    failed_items.append(f"media {m.message_id}: {mr.error}")
        row.update(
            {
                "chatwoot_live_messages": live_messages,
                "chatwoot_live_attachments": live_attachments,
                "chatwoot_live_status": live_status,
                "failed_items": " | ".join(failed_items),
            }
        )
        rows.append(row)
    _write_csv(out_dir / "validation_report.csv", rows, SUMMARY_FIELDS + ["chatwoot_live_messages", "chatwoot_live_attachments", "chatwoot_live_status", "failed_items"])
    return rows


def print_validation(rows: list[dict]) -> None:
    for r in rows:
        print("-" * 72)
        print(f"respond.io contact ID      : {r['respond_contact_id']}  ({r['contact_name']} {r['phone']})")
        print(f"Chatwoot contact ID        : {r['chatwoot_contact_id'] or '-'}  [{r['contact_action'] or r['contact_status']}]")
        print(f"Chatwoot conversation ID(s): {r['chatwoot_conversation_ids'] or '-'}  status: {r['conversation_statuses']}")
        print(f"source messages / imported : {r['source_messages']} / {r['imported_messages']}  (failed {r['failed_messages']})")
        print(f"source media / imported    : {r['source_media']} / {r['imported_media']}")
        if r.get("chatwoot_live_messages") != "":
            print(f"Chatwoot live message count: {r.get('chatwoot_live_messages')}  attachments: {r.get('chatwoot_live_attachments')}  status: {r.get('chatwoot_live_status')}")
        print(f"failed items               : {r.get('failed_items') or 'none'}")
    print("-" * 72)


def plan_overview(plan: MigrationPlan) -> dict:
    s = plan.stats
    return {
        "contacts": len(plan.contacts),
        "contacts_with_messages": s.get("contacts_with_messages"),
        "conversations": s.get("conversations_total"),
        "conversations_synthetic": s.get("conversations_synthetic"),
        "messages": s.get("messages_unique"),
        "messages_incoming": s.get("messages_incoming"),
        "messages_outgoing": s.get("messages_outgoing"),
        "media": s.get("media_total"),
    }
