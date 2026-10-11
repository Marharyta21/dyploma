"""Fail-closed checks that protect the live WhatsApp inbox.

Nothing is written to Chatwoot unless every check in ``run_connection_checks``
passes, and ``assert_archive_inbox`` is re-applied to every conversation the
migrator touches.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .chatwoot import ChatwootClient, ChatwootHTTPError
from .config import Config

API_CHANNEL = "Channel::Api"
WHATSAPP_CHANNELS = {"Channel::Whatsapp"}
CONTACT_ATTRIBUTE_KEY = "respondio_contact_id"


class SafetyError(Exception):
    pass


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    warning: bool = False


@dataclass
class CheckReport:
    results: list[CheckResult] = field(default_factory=list)
    inbox: Optional[dict] = None
    whatsapp_inboxes: list[dict] = field(default_factory=list)
    custom_attribute_defined: bool = False

    @property
    def ok(self) -> bool:
        return all(r.ok or r.warning for r in self.results)

    def add(self, name: str, ok: bool, detail: str, warning: bool = False) -> None:
        self.results.append(CheckResult(name, ok, detail, warning))


def is_whatsapp_inbox(inbox: dict) -> bool:
    ctype = inbox.get("channel_type", "")
    if ctype in WHATSAPP_CHANNELS:
        return True
    if ctype == "Channel::TwilioSms" and str(inbox.get("medium", "")).lower() == "whatsapp":
        return True
    return False


def assert_archive_inbox(cfg: Config, inbox: dict) -> None:
    """Raise unless ``inbox`` is the dedicated API archive inbox."""
    if int(inbox.get("id", -1)) != cfg.archive_inbox_id:
        raise SafetyError(f"inbox id {inbox.get('id')} is not the configured archive inbox {cfg.archive_inbox_id}")
    if inbox.get("channel_type") != API_CHANNEL:
        raise SafetyError(
            f"inbox {inbox.get('id')} ({inbox.get('name')!r}) has channel_type {inbox.get('channel_type')!r}; "
            f"only {API_CHANNEL} is allowed for historical imports"
        )
    if is_whatsapp_inbox(inbox):
        raise SafetyError("refusing to use a WhatsApp inbox for historical messages")
    expected = cfg.archive_inbox_name.strip().lower()
    if expected and str(inbox.get("name", "")).strip().lower() != expected:
        raise SafetyError(
            f"inbox {inbox.get('id')} is named {inbox.get('name')!r} but CHATWOOT_ARCHIVE_INBOX_NAME is {cfg.archive_inbox_name!r}"
        )


def assert_conversation_in_archive(cfg: Config, conversation: dict) -> None:
    inbox_id = conversation.get("inbox_id")
    if inbox_id is None and isinstance(conversation.get("meta"), dict):
        inbox_id = (conversation["meta"].get("inbox") or {}).get("id")
    if inbox_id is None or int(inbox_id) != cfg.archive_inbox_id:
        raise SafetyError(
            f"conversation {conversation.get('id')} belongs to inbox {inbox_id}, not the archive inbox {cfg.archive_inbox_id}"
        )


def run_connection_checks(client: ChatwootClient, cfg: Config, logger: Optional[logging.Logger] = None) -> CheckReport:
    log = logger or logging.getLogger("migration")
    report = CheckReport()

    # 1. token
    try:
        profile = client.get_profile()
        who = profile.get("email") or profile.get("name") or "?"
        report.add("api_token", True, f"token belongs to {who} (user id {profile.get('id')})")
    except ChatwootHTTPError as exc:
        report.add("api_token", False, f"token rejected: HTTP {exc.status}")
        return report
    except Exception as exc:  # noqa: BLE001
        report.add("api_token", False, f"cannot reach {cfg.base_url}: {exc}")
        return report

    # 2. account
    accounts = {int(a.get("id")): a for a in profile.get("accounts", []) if a.get("id") is not None}
    if cfg.account_id in accounts:
        acct = accounts[cfg.account_id]
        report.add("account_id", True, f"account {cfg.account_id} ({acct.get('name')!r}, role {acct.get('role')})")
    else:
        report.add("account_id", False, f"account {cfg.account_id} is not among this token's accounts {sorted(accounts)}")
        return report

    # 3. archive inbox
    try:
        inbox = client.get_inbox(cfg.archive_inbox_id)
    except ChatwootHTTPError as exc:
        report.add("archive_inbox_exists", False, f"GET inbox {cfg.archive_inbox_id} -> HTTP {exc.status}")
        return report
    report.inbox = inbox
    report.add("archive_inbox_exists", True, f"inbox {inbox.get('id')} {inbox.get('name')!r} channel_type={inbox.get('channel_type')}")
    try:
        assert_archive_inbox(cfg, inbox)
        report.add("archive_inbox_is_api", True, f"channel_type is {API_CHANNEL} and the name matches {cfg.archive_inbox_name!r}")
    except SafetyError as exc:
        report.add("archive_inbox_is_api", False, str(exc))
    if inbox.get("lock_to_single_conversation"):
        report.add(
            "archive_inbox_not_locked",
            False,
            "lock_to_single_conversation is enabled on the archive inbox; disable it or every historical conversation "
            "would be merged into one",
        )
    else:
        report.add("archive_inbox_not_locked", True, "lock_to_single_conversation is off")
    if inbox.get("callback_webhook_url"):
        report.add("archive_inbox_webhook", True, f"inbox webhook {inbox.get('callback_webhook_url')} will receive events for imported messages", warning=True)

    # 4. other inboxes (identify live WhatsApp inboxes)
    try:
        inboxes = client.list_inboxes()
    except ChatwootHTTPError as exc:
        report.add("list_inboxes", False, f"HTTP {exc.status}")
        return report
    wa = [i for i in inboxes if is_whatsapp_inbox(i)]
    report.whatsapp_inboxes = wa
    names = ", ".join(f"{i.get('id')} {i.get('name')!r}" for i in wa) or "none found"
    if any(int(i.get("id", -1)) == cfg.archive_inbox_id for i in wa):
        report.add("archive_not_whatsapp", False, f"archive inbox id is a WhatsApp inbox! ({names})")
    else:
        report.add("archive_not_whatsapp", True, f"WhatsApp inboxes that will NOT be touched: {names}")

    # 5. custom attribute definition (informational)
    try:
        defs = client.list_custom_attribute_definitions("contact_attribute")
        keys = {d.get("attribute_key") for d in defs}
        report.custom_attribute_defined = CONTACT_ATTRIBUTE_KEY in keys
        if report.custom_attribute_defined:
            report.add("custom_attribute", True, f"contact attribute {CONTACT_ATTRIBUTE_KEY!r} exists")
        else:
            report.add(
                "custom_attribute",
                True,
                f"contact attribute {CONTACT_ATTRIBUTE_KEY!r} not defined yet; it will be created when the migration runs",
                warning=True,
            )
    except ChatwootHTTPError as exc:
        report.add("custom_attribute", True, f"could not list custom attributes (HTTP {exc.status})", warning=True)

    for r in report.results:
        level = logging.INFO if r.ok else logging.ERROR
        log.log(level, "check %-26s %s %s", r.name, "WARN" if r.warning else ("OK  " if r.ok else "FAIL"), r.detail)
    return report
