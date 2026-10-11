#!/usr/bin/env python3
"""respond.io -> Chatwoot historical migration CLI.

Stages
  A  --analyze                     inspect the CSV exports (no network)
  B  --media-test                  download ~10 sample media files
  C  --check                       verify token, account, archive inbox (read-only)
  D  --test                        migrate ONE contact / conversation and stop
  E  --validate                    (re)print the validation report
  F  --full --confirm-full-migration   migrate everything (only after approval)

  --dry-run can be combined with --test or --full: it reads everything, validates
  mappings and prints what would be created without any Chatwoot write.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from migration.analysis import run_analysis
from migration.assembler import build_plan, select_test_contact
from migration.chatwoot import ChatwootClient, WriteBlocked
from migration.config import Config, ConfigError, load_config
from migration.csv_loader import CsvSchemaError, load_data_dir
from migration.logging_setup import setup_logging
from migration.media import MediaDownloader, run_media_test
from migration.migrator import Migrator
from migration.reports import plan_overview, print_validation, validation_report, write_failed_reports, write_summary
from migration.safety import SafetyError, run_connection_checks
from migration.state import StateDB, StateSchemaError


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--analyze", action="store_true", help="Stage A: analyse the CSV exports only")
    mode.add_argument("--media-test", action="store_true", help="Stage B: download a sample of ~10 media files")
    mode.add_argument("--check", action="store_true", help="Stage C: verify Chatwoot credentials and the archive inbox")
    mode.add_argument("--test", action="store_true", help="Stage D+E: migrate one test contact and stop")
    mode.add_argument("--validate", action="store_true", help="Stage E: print the validation report for migrated contacts")
    mode.add_argument("--full", action="store_true", help="Stage F: migrate everything (requires --confirm-full-migration)")
    ap.add_argument("--dry-run", action="store_true", help="No Chatwoot writes; show what would be created (use with --test or --full)")
    ap.add_argument("--confirm-full-migration", action="store_true", help="Required together with --full")
    ap.add_argument("--test-contact", help="respond.io Contact ID to use for --test / --validate (default: auto-select)")
    ap.add_argument("--media-sample", type=int, default=10, help="How many media files to download in --media-test")
    ap.add_argument("--placeholder-for-failed-media", action="store_true", help="Import a text placeholder instead of stopping the conversation when a media download fails")
    ap.add_argument("--limit", type=int, default=0, help="Only process the first N contacts (for --full, useful for batching)")
    ap.add_argument("--data-dir", help="Folder with the respond.io CSV exports (default: ./data)")
    ap.add_argument("--media-dir", help="Folder for downloaded media (default: ./media)")
    ap.add_argument("--output-dir", help="Folder for logs, state DB and reports (default: ./output)")
    ap.add_argument("--env-file", help="Path to the .env file (default: ./.env)")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def build_client(cfg: Config, allow_writes: bool, logger) -> ChatwootClient:
    cfg.require_chatwoot()
    return ChatwootClient(
        cfg.base_url,
        cfg.account_id,
        cfg.api_token,
        allow_writes=allow_writes,
        requests_per_minute=cfg.requests_per_minute,
        timeout=cfg.http_timeout,
        logger=logger,
    )


def load_plan(cfg: Config, logger):
    raw = load_data_dir(cfg.data_dir, cfg.tz, logger)
    plan = build_plan(raw, logger)
    return raw, plan


def stage_c(cfg: Config, logger, client: ChatwootClient | None = None) -> bool:
    client = client or build_client(cfg, allow_writes=False, logger=logger)
    print("\nSTAGE C - Chatwoot connection and safety checks (read-only)")
    report = run_connection_checks(client, cfg, logger)
    for r in report.results:
        flag = "WARN" if r.warning else ("OK  " if r.ok else "FAIL")
        print(f"  [{flag}] {r.name}: {r.detail}")
    if report.ok:
        print("  All checks passed. Archive inbox is an API inbox; WhatsApp inboxes will not be touched.")
    else:
        print("  CHECKS FAILED - nothing will be written to Chatwoot.")
    return report.ok


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(
            Path(args.env_file) if args.env_file else None,
            overrides={"DATA_DIR": args.data_dir, "MEDIA_DIR": args.media_dir, "OUTPUT_DIR": args.output_dir},
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(cfg.log_path, verbose=args.verbose)
    mode = next(m for m in ("analyze", "media_test", "check", "test", "validate", "full") if getattr(args, m))
    logger.info("=== migrate.py --%s%s ===", mode.replace("_", "-"), " --dry-run" if args.dry_run else "")

    try:
        if args.check:
            return 0 if stage_c(cfg, logger) else 1

        raw, plan = load_plan(cfg, logger)

        if args.analyze:
            run_analysis(raw, plan, cfg.output_dir, cfg.source_timezone, logger)
            return 0

        state = StateDB(cfg.state_db_path)

        if args.media_test:
            downloader = MediaDownloader(cfg.media_dir, state, logger, timeout=cfg.media_timeout)
            rows = run_media_test(plan, downloader, cfg.output_dir / "media_test_report.csv", n=args.media_sample, logger=logger)
            ok = sum(1 for r in rows if r["status"] == "success")
            print(f"\nSTAGE B - media download test: {ok}/{len(rows)} succeeded")
            for r in rows:
                print(f"  [{'OK  ' if r['status'] == 'success' else 'FAIL'}] {r['kind']:<8} {r['file_name'] or r['url'][:60]:<40} {r['bytes']:>9} bytes  {r['error']}")
            print(f"  report: {cfg.output_dir / 'media_test_report.csv'}")
            write_failed_reports(state, cfg.output_dir)
            return 0 if ok == len(rows) else 1

        # ---- scope -----------------------------------------------------------
        if args.test or (args.validate and args.test_contact):
            selected = [select_test_contact(plan, args.test_contact)]
        elif args.validate:
            migrated = {c.respond_contact_id for c in state.contacts()}
            selected = [p for cid, p in plan.contacts.items() if cid in migrated]
        else:
            selected = [p for p in plan.contacts.values() if p.message_count]
            if args.limit:
                selected = selected[: args.limit]

        if args.validate:
            client = build_client(cfg, allow_writes=False, logger=logger) if cfg.api_token else None
            rows = validation_report(selected, state, client, cfg.output_dir, logger)
            print_validation(rows)
            write_failed_reports(state, cfg.output_dir)
            return 0

        if args.full and not args.dry_run and not args.confirm_full_migration:
            print("Refusing to run the full migration without --confirm-full-migration.", file=sys.stderr)
            return 2

        overview = plan_overview(plan)
        print("\nSource overview: " + json.dumps(overview))
        if args.test:
            tp = selected[0]
            conv = tp.conversations[0] if tp.conversations else None
            print(
                f"Test contact: {tp.contact.contact_id} ({tp.contact.name}, {tp.contact.phone_e164}) - "
                f"{len(tp.conversations)} conversation(s), {tp.message_count} messages, {tp.media_count} media"
            )
            if conv:
                for m in conv.messages:
                    media = f" media={m.media.kind}" if m.media else ""
                    print(f"    {m.local_time}  {m.direction:<8} {m.content_type:<18} {m.message_id}{media}")

        # ---- Stage C gate ---------------------------------------------------------
        if not cfg.api_token and args.dry_run:
            print("No Chatwoot credentials in .env: dry-run continues without the connection checks.")
            client = None
        else:
            client = build_client(cfg, allow_writes=not args.dry_run, logger=logger)
            if not stage_c(cfg, logger, client):
                return 1

        downloader = MediaDownloader(cfg.media_dir, state, logger, timeout=cfg.media_timeout)
        migrator = Migrator(
            cfg,
            client if client is not None else ChatwootClient("http://invalid", 0, "", allow_writes=False, logger=logger),
            state,
            downloader,
            dry_run=args.dry_run,
            logger=logger,
            placeholder_for_failed_media=args.placeholder_for_failed_media,
        )

        if args.dry_run:
            for p in selected:
                migrator.migrate_contact_plan(p)
            w = migrator.stats.would_create
            print("\nDRY RUN - nothing was written to Chatwoot.")
            print(f"  contacts to create/lookup     : {w['contacts']}   (already mapped: {w['contacts_already_mapped']})")
            print(f"  conversations to create       : {w['conversations']}   (already mapped: {w['conversations_already_mapped']})")
            print(f"  messages to create            : {w['messages']}   (already imported: {w['messages_already_mapped']})")
            print(f"  media files to download/upload: {w['media_downloads']}")
            print(f"  conversations to resolve      : {w['conversations_to_resolve']}")
            print(f"  Chatwoot write calls made     : {client.write_count if client else 0}")
            return 0

        if args.full:
            print(f"\nSTAGE F - FULL MIGRATION of {len(selected)} contacts into archive inbox {cfg.archive_inbox_id}")
        else:
            print(f"\nSTAGE D - TEST MIGRATION of contact {selected[0].contact.contact_id} into archive inbox {cfg.archive_inbox_id}")
        run_id = state.start_run(mode)
        try:
            for idx, p in enumerate(selected, start=1):
                migrator.migrate_contact_plan(p)
                if args.full and idx % 50 == 0:
                    logger.info("Progress: %d/%d contacts, stats=%s", idx, len(selected), json.dumps(migrator.stats.as_dict()))
        finally:
            state.finish_run(run_id, json.dumps(migrator.stats.as_dict()))
            write_summary(selected, state, cfg.output_dir)
            nmsg, nmedia = write_failed_reports(state, cfg.output_dir)
            print("\nRun statistics: " + json.dumps(migrator.stats.as_dict(), indent=2))
            print(f"failed messages: {nmsg}  failed media: {nmedia}  (see {cfg.output_dir}/failed_messages.csv, failed_media.csv)")

        print("\nSTAGE E - validation report")
        rows = validation_report(selected, state, client, cfg.output_dir, logger)
        print_validation(rows)
        print(f"Reports written to {cfg.output_dir}: migration_summary.csv, validation_report.csv, failed_messages.csv, failed_media.csv, migration.log")
        if args.test:
            print("\nTest migration finished. Open the conversation in Chatwoot, verify it, then run the full migration only when you are satisfied.")
        return 0 if migrator.stats.contacts_failed == 0 and migrator.stats.conversations_failed == 0 else 1

    except CsvSchemaError as exc:
        logger.error("CSV problem: %s", exc)
        return 2
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 2
    except StateSchemaError as exc:
        logger.error("State database problem: %s", exc)
        return 2
    except SafetyError as exc:
        logger.error("SAFETY STOP: %s", exc)
        return 3
    except WriteBlocked as exc:
        logger.error("Write blocked in dry-run: %s", exc)
        return 3
    except KeyboardInterrupt:
        logger.warning("Interrupted; state is saved, re-run to resume")
        return 130


if __name__ == "__main__":
    sys.exit(main())
