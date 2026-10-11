"""Plain data structures shared by every stage of the migration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


@dataclass
class MediaRef:
    """A media file referenced by a respond.io message payload."""

    url: str
    kind: str  # image | video | audio | file | sticker
    mime: str
    file_name: str
    ext: str
    size: Optional[int]


@dataclass
class SourceContact:
    contact_id: str
    first_name: str = ""
    last_name: str = ""
    phone_raw: str = ""
    email: str = ""
    country: str = ""
    language: str = ""
    tags: str = ""
    status: str = ""
    lifecycle: str = ""
    assignee: str = ""
    channels: str = ""
    created_at: Optional[datetime] = None
    last_interaction_at: Optional[datetime] = None
    extra: dict[str, str] = field(default_factory=dict)
    placeholder: bool = False  # referenced by messages/conversations but absent from the contacts CSV
    source_file: str = ""
    row_number: int = 0

    @property
    def name(self) -> str:
        name = f"{self.first_name} {self.last_name}".strip()
        if name:
            return name
        if self.phone_e164:
            return self.phone_e164
        return f"respond.io contact {self.contact_id}"

    @property
    def phone_e164(self) -> Optional[str]:
        """respond.io exports digits only; Chatwoot requires E.164 with a leading plus."""
        digits = re.sub(r"\D", "", self.phone_raw or "")
        if not digits:
            return None
        candidate = f"+{digits}"
        return candidate if E164_RE.match(candidate) else None

    @property
    def identifier(self) -> str:
        return f"respondio:{self.contact_id}"


@dataclass
class SourceMessage:
    message_id: str
    contact_id: str
    sender_id: str
    sender_type: str
    content_type: str
    direction: str  # incoming | outgoing
    raw_content: str
    payload: dict[str, Any]
    channel_id: str
    type_col: str
    sub_type: str
    timestamp: datetime  # timezone-aware, UTC
    local_time: str  # the original "Date & Time" string from the export
    media: Optional[MediaRef] = None
    source_file: str = ""
    row_number: int = 0
    conversation_id: Optional[str] = None  # assigned by the assembler

    @property
    def is_incoming(self) -> bool:
        return self.direction == "incoming"

    @property
    def key(self) -> str:
        """Unique key: respond.io message IDs are second-precision and can collide across contacts."""
        return f"{self.contact_id}:{self.message_id}"

    @property
    def chatwoot_source_id(self) -> str:
        return f"respondio:{self.contact_id}:{self.message_id}"


@dataclass
class SourceConversation:
    conversation_id: str
    contact_id: str
    started_at: datetime
    resolved_at: Optional[datetime]
    started_local: str = ""
    resolved_local: str = ""
    assignee: str = ""
    closed_by: str = ""
    category: str = ""
    closing_note: str = ""
    opened_by_channel: str = ""
    n_incoming: Optional[int] = None
    n_outgoing: Optional[int] = None
    synthetic: bool = False  # created because the contact has messages but no conversation row
    source_file: str = ""
    row_number: int = 0
    messages: list[SourceMessage] = field(default_factory=list)

    @property
    def media_count(self) -> int:
        return sum(1 for m in self.messages if m.media is not None)


@dataclass
class ContactPlan:
    contact: SourceContact
    conversations: list[SourceConversation] = field(default_factory=list)

    @property
    def message_count(self) -> int:
        return sum(len(c.messages) for c in self.conversations)

    @property
    def media_count(self) -> int:
        return sum(c.media_count for c in self.conversations)


@dataclass
class Issue:
    kind: str
    entity: str
    entity_id: str
    detail: str


@dataclass
class MigrationPlan:
    contacts: dict[str, ContactPlan]
    issues: list[Issue]
    stats: dict[str, Any]

    def iter_conversations(self):
        for plan in self.contacts.values():
            for conv in plan.conversations:
                yield plan, conv

    def iter_messages(self):
        for plan, conv in self.iter_conversations():
            for msg in conv.messages:
                yield plan, conv, msg
