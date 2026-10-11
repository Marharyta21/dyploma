"""Convert a respond.io message payload into Chatwoot message content."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .models import MediaRef, SourceMessage

MAX_CONTENT = 150_000  # Chatwoot validates content length <= 150000
UNSUPPORTED_TEXT = "[Unsupported WhatsApp message type - content was not exported by respond.io]"


@dataclass
class Rendered:
    content: str
    media: Optional[MediaRef]
    unsupported: bool
    kind: str
    template_name: str = ""


def _template_text(template: dict[str, Any]) -> str:
    parts: list[str] = []
    for comp in template.get("components", []) or []:
        ctype = str(comp.get("type", "")).lower()
        if ctype == "header":
            text = comp.get("text")
            if text:
                parts.append(f"*{text}*")
        elif ctype == "body":
            if comp.get("text"):
                parts.append(str(comp["text"]))
        elif ctype == "footer":
            if comp.get("text"):
                parts.append(f"_{comp['text']}_")
        elif ctype == "buttons":
            labels = []
            for b in comp.get("buttons", []) or comp.get("parameters", []) or []:
                if isinstance(b, dict):
                    label = b.get("text") or b.get("title") or b.get("payload")
                    if label:
                        labels.append(str(label))
            if labels:
                parts.append("[Buttons: " + " | ".join(labels) + "]")
    if not parts:
        name = template.get("name", "")
        parts.append(f"[WhatsApp template: {name}]" if name else "[WhatsApp template]")
    return "\n\n".join(parts)


def render(msg: SourceMessage, timestamp_prefix: bool = False, tz: Optional[ZoneInfo] = None) -> Rendered:
    p = msg.payload or {}
    ptype = str(p.get("type") or msg.content_type or "").lower()
    media = msg.media
    unsupported = False
    template_name = ""

    if ptype == "text":
        content = str(p.get("text") or "")
        kind = "text"
    elif ptype == "quick_reply":
        title = str(p.get("title") or "")
        replies = [str(r) for r in p.get("replies", []) or []]
        content = title
        if replies:
            content = (content + "\n\n" if content else "") + "[Quick replies: " + " | ".join(replies) + "]"
        kind = "quick_reply"
    elif ptype == "whatsapp_template":
        template = p.get("template") or {}
        template_name = str(template.get("name") or "")
        content = _template_text(template)
        kind = "whatsapp_template"
    elif ptype == "attachment":
        content = str(p.get("text") or p.get("caption") or "")
        kind = "attachment"
    elif ptype == "sticker":
        content = ""
        kind = "sticker"
    elif ptype == "location":
        lat, lon = p.get("latitude"), p.get("longitude")
        address = str(p.get("address") or "").strip()
        content = "📍 Location"
        if address:
            content += f": {address}"
        if lat is not None and lon is not None:
            content += f"\nhttps://maps.google.com/?q={lat},{lon}"
        kind = "location"
    elif ptype == "contact":
        c = p.get("contact") or {}
        name = f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
        phone = str(c.get("phone") or "")
        content = "👤 Contact card: " + " ".join(x for x in (name, phone) if x)
        kind = "contact"
    elif ptype == "unsupported":
        content = UNSUPPORTED_TEXT
        kind = "unsupported"
        unsupported = True
    elif ptype == "raw":
        content = str(p.get("text") or "")
        kind = "raw"
    else:
        content = json.dumps(p, ensure_ascii=False)
        kind = ptype or "unknown"
        unsupported = True

    if media and not content and kind in {"attachment", "sticker"}:
        # Attachment-only message; Chatwoot renders the file, so content stays empty.
        content = ""

    if timestamp_prefix:
        local = msg.timestamp.astimezone(tz) if tz else msg.timestamp
        stamp = local.strftime("%Y-%m-%d %H:%M")
        content = f"🕓 {stamp}\n{content}" if content else f"🕓 {stamp}"

    if len(content) > MAX_CONTENT:
        content = content[: MAX_CONTENT - 20] + "\n[...truncated...]"

    return Rendered(content=content, media=media, unsupported=unsupported, kind=kind, template_name=template_name)


def placeholder_for_missing_media(rendered: Rendered, media: MediaRef, reason: str) -> str:
    label = media.file_name or media.url.rsplit("/", 1)[-1]
    note = f"[Media not available: {media.kind} {label} - {reason}]"
    return f"{rendered.content}\n{note}" if rendered.content else note
