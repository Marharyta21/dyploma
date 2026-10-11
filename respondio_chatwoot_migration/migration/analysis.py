"""Stage A: inspect the exports and produce a migration summary (no network)."""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .assembler import SUPPORTED_CONTENT_TYPES
from .csv_loader import RawData
from .models import MigrationPlan


def infer_utc_offset_minutes(raw: RawData) -> Optional[int]:
    """Unsupported-message payloads carry a WhatsApp UTC epoch; compare it with the export's
    local 'Date & Time' to learn the export timezone offset."""
    diffs: Counter[int] = Counter()
    for m in raw.messages:
        if m.content_type != "unsupported":
            continue
        payload = m.payload.get("payload") or {}
        ts = payload.get("timestamp")
        if not ts:
            continue
        try:
            wa_utc = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            local_naive = datetime.strptime(m.local_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        diffs[round((local_naive - wa_utc).total_seconds() / 60)] += 1
    if not diffs:
        return None
    return diffs.most_common(1)[0][0]


def run_analysis(raw: RawData, plan: MigrationPlan, out_dir: Path, source_tz: str, logger: Optional[logging.Logger] = None) -> dict:
    log = logger or logging.getLogger("migration")
    out_dir.mkdir(parents=True, exist_ok=True)
    s = plan.stats
    lines: list[str] = []

    def p(line: str = "") -> None:
        lines.append(line)
        print(line)

    p("=" * 78)
    p("STAGE A - respond.io export analysis")
    p("=" * 78)
    for f in raw.files:
        p(f"\n{f.path.name}  [{f.kind}]  rows={f.rows}")
        p("  columns: " + ", ".join(f.columns))

    offset = infer_utc_offset_minutes(raw)
    p("")
    if offset is not None:
        sign = "+" if offset >= 0 else "-"
        p(f"Export timezone inferred from WhatsApp payload timestamps: UTC{sign}{abs(offset)//60:02d}:{abs(offset)%60:02d}  (configured SOURCE_TIMEZONE={source_tz})")
        tz_offset_now = datetime.now(plan_tz(source_tz)).utcoffset()
        if tz_offset_now is not None and round(tz_offset_now.total_seconds() / 60) != offset:
            p(f"  WARNING: SOURCE_TIMEZONE {source_tz} has offset {tz_offset_now}; this does not match the inferred offset. Fix SOURCE_TIMEZONE in .env.")
    else:
        p(f"Export timezone could not be inferred; using SOURCE_TIMEZONE={source_tz}")

    p("")
    p("Counts")
    p(f"  contacts rows / unique            : {s['contacts_rows']} / {s['contacts_unique']}")
    p(f"  contacts referenced but missing   : {s['contacts_placeholder']} (placeholder contacts will be created)")
    p(f"  contacts without phone            : {s['contacts_without_phone']}   with email: {s['contacts_with_email']}")
    p(f"  contacts with messages / without  : {s['contacts_with_messages']} / {s['contacts_without_messages']}")
    p(f"  conversations rows / unique       : {s['conversations_rows']} / {s['conversations_unique']}")
    p(f"  conversation duplicates           : {s['conversations_duplicate_identical']} identical, {s['conversations_duplicate_conflicting']} conflicting")
    p(f"  synthetic conversations           : {s['conversations_synthetic']} (contacts with messages but no conversation row)")
    p(f"  conversations without messages    : {s['conversations_without_messages']} (nothing to import for them)")
    p(f"  messages rows / unique            : {s['messages_rows']} / {s['messages_unique']}")
    p(f"  message duplicates                : {s['messages_duplicate_identical']} identical, {s['messages_duplicate_conflicting']} conflicting (keyed by contact id + message id; first occurrence kept)")
    p(f"  message ids shared by 2+ contacts : {s['message_ids_shared_between_contacts']} (kept as separate messages)")
    p(f"  messages incoming / outgoing      : {s['messages_incoming']} / {s['messages_outgoing']}")
    if s.get("messages_date_range"):
        p(f"  message date range (UTC)          : {s['messages_date_range'][0]} .. {s['messages_date_range'][1]}")
    p(f"  message placement                 : {json.dumps(s['message_placement'])}")
    p(f"  media messages                    : {s['media_total']}  by kind {json.dumps(s['media_by_kind'])}")
    p(f"  media hosts                       : {json.dumps(s['media_hosts'])}")
    p(f"  parse errors                      : {s['parse_errors']}")

    p("")
    p("Message content types (respond.io 'Content Type')")
    for k, v in sorted(s["content_types"].items(), key=lambda kv: -kv[1]):
        how = {
            "text": "text",
            "quick_reply": "text + listed quick replies",
            "whatsapp_template": "rendered template body/buttons as text",
            "attachment": "attachment upload (image/video/audio/file)",
            "sticker": "image attachment (webp)",
            "location": "text with address + Google Maps link",
            "contact": "text contact card",
            "unsupported": "placeholder text (respond.io did not export the content)",
        }.get(k, "UNSUPPORTED -> will be imported as raw JSON text")
        p(f"  {k:<20} {v:>7}   -> {how}")
    unsupported = {k: v for k, v in s["content_types"].items() if k not in SUPPORTED_CONTENT_TYPES}
    p(f"  not natively supported: {json.dumps(unsupported) if unsupported else 'none'}")
    p("")
    p("Sender types: " + json.dumps(s["sender_types"]))
    p("Channel IDs : " + json.dumps(s["channel_ids"]))

    p("")
    p("Issues (first 15, full list in analysis_issues.csv)")
    kinds = Counter(i.kind for i in plan.issues)
    for k, v in kinds.most_common():
        p(f"  {k:<36} {v}")
    for i in plan.issues[:15]:
        p(f"    - {i.kind} {i.entity} {i.entity_id}: {i.detail}")

    with (out_dir / "analysis_issues.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["kind", "entity", "entity_id", "detail"])
        for i in plan.issues:
            w.writerow([i.kind, i.entity, i.entity_id, i.detail])

    summary = {
        "files": [{"name": f.path.name, "kind": f.kind, "rows": f.rows, "columns": f.columns} for f in raw.files],
        "inferred_utc_offset_minutes": offset,
        "stats": s,
        "issue_counts": dict(kinds),
    }
    (out_dir / "analysis_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (out_dir / "analysis_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (out_dir / "analysis_summary.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["metric", "value"])
        for k, v in s.items():
            w.writerow([k, json.dumps(v) if isinstance(v, (dict, list, tuple)) else v])
    log.info("Analysis written to %s", out_dir)
    return summary


def plan_tz(name: str):
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)
