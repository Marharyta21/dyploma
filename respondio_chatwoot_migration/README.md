# respond.io → Chatwoot historical migration

Imports historical WhatsApp conversations exported from respond.io (CSV) into a
**dedicated Chatwoot API inbox** (`Respond.io Archive`) through the Chatwoot
Application API. Nothing is ever sent to a customer: the live WhatsApp inbox is
never used, and the tool refuses to run if the configured inbox is not an API
inbox with the expected name.

```
respond.io contact → Chatwoot contact → contact inbox (source) in the archive API inbox
→ historical conversation → incoming/outgoing messages (chronological) → attachments
→ conversation marked resolved
```

## Safety model

* The archive inbox is verified before every write session: it must exist, have
  `channel_type == "Channel::Api"`, carry the name in `CHATWOOT_ARCHIVE_INBOX_NAME`
  and must not have *lock to single conversation* enabled. All WhatsApp inboxes
  of the account are listed and the archive id must not be one of them.
* Every conversation the tool creates or reuses is checked again
  (`inbox_id == archive inbox`) before messages are posted.
* `--dry-run` uses a read-only HTTP client; any non-GET call raises an error.
* `--full` refuses to run without `--confirm-full-migration`.
* Credentials live in `.env` only (`.env` is git-ignored, `.env.example` is committed).
* respond.io is only *read* (CSV files on disk). Nothing is deleted anywhere.

## Setup (macOS, step by step)

```bash
# 1. go to the project folder
cd respondio_chatwoot_migration

# 2. create and activate a virtual environment (Python 3.11 or newer)
python3 -m venv .venv
source .venv/bin/activate

# 3. install dependencies
pip install -r requirements.txt

# 4. create your .env from the template and fill in the values
cp .env.example .env
open -e .env        # or: nano .env

# 5. put the five respond.io CSV exports into ./data  (file names do not matter)
mkdir -p data
cp ~/Downloads/*.csv data/
```

`.env` needs at least:

```
CHATWOOT_BASE_URL=https://app.chatwoot.com
CHATWOOT_ACCOUNT_ID=185855
CHATWOOT_API_TOKEN=<your token>
CHATWOOT_ARCHIVE_INBOX_ID=145492
CHATWOOT_ARCHIVE_INBOX_NAME=Respond.io Archive
SOURCE_TIMEZONE=Asia/Dubai
```

## Stages and commands

| Stage | Command | Writes to Chatwoot? |
|---|---|---|
| A – analysis | `python migrate.py --analyze` | no (no network at all) |
| B – media download test (≈10 files) | `python migrate.py --media-test` | no |
| C – connection & safety checks | `python migrate.py --check` | no (GET only) |
| dry run | `python migrate.py --dry-run --test` / `--dry-run --full` | no (writes are blocked) |
| D+E – test migration + validation | `python migrate.py --test` | yes: 1 contact, 1 conversation, ~10 messages |
| E – re-print validation report | `python migrate.py --validate` | no |
| F – full migration | `python migrate.py --full --confirm-full-migration` | yes: everything |

Useful options:

* `--test-contact <respond.io ContactID>` choose the test contact yourself (default: auto-picks a
  contact with one conversation of ~10 messages including incoming, outgoing and a media file).
* `--placeholder-for-failed-media` when a media URL is expired, import a text note
  `[Media not available: …]` instead of stopping that conversation (default: stop and report, so
  nothing is silently skipped; re-run later to retry).
* `--limit N` process only the first N contacts (batching the full run).
* `--data-dir`, `--media-dir`, `--output-dir`, `--env-file`, `-v`.

All output goes to `output/`: `migration.log`, `migration_state.db` (SQLite), `failed_messages.csv`,
`failed_media.csv`, `migration_summary.csv`, `validation_report.csv`, `analysis_summary.*`,
`analysis_issues.csv`, `media_test_report.csv`. Downloaded media goes to `media/`.

## Recommended run order

1. `python migrate.py --analyze` – check the counts and the inferred timezone.
2. `python migrate.py --media-test` – confirm the respond.io CDN links still work.
3. `python migrate.py --check` – token, account, archive inbox, WhatsApp inboxes untouched.
4. `python migrate.py --dry-run --test` – see exactly what the test run will create.
5. `python migrate.py --test` – creates the test conversation and prints the validation report.
6. **Open the conversation in Chatwoot and verify it.** Only then:
7. `python migrate.py --dry-run --full`, then `python migrate.py --full --confirm-full-migration`.
8. If interrupted or if some items failed: fix the cause and re-run the same command. Already
   imported items are skipped (state DB + lookups in Chatwoot).

## Architecture

```
migrate.py                 CLI, stage orchestration, exit codes
migration/config.py        .env loading and validation
migration/csv_loader.py    header-based detection of the 3 export kinds, parsing, timezone → UTC
migration/assembler.py     dedupe, message→conversation linking, synthetic conversations, ordering
migration/renderer.py      respond.io payload JSON → Chatwoot message text (+ media reference)
migration/analysis.py      Stage A report
migration/media.py         downloader with retries/backoff, Stage B sample test
migration/chatwoot.py      API client: rate limiting, retries, Retry-After, read-only guard
migration/safety.py        Stage C checks and fail-closed inbox assertions
migration/state.py         SQLite mappings (contacts, conversations, messages, media, runs)
migration/migrator.py      Stage D/F engine (idempotent, resumable)
migration/reports.py       CSV reports and Stage E validation
tests/fake_chatwoot.py     in-memory Chatwoot look-alike for tests and rehearsals
```

## Chatwoot endpoints used (verified against the official OpenAPI spec and server source)

| Purpose | Endpoint |
|---|---|
| verify token / list accounts | `GET /api/v1/profile` |
| archive inbox details | `GET /api/v1/accounts/{account_id}/inboxes/{id}` |
| list inboxes (find WhatsApp inboxes) | `GET /api/v1/accounts/{account_id}/inboxes` |
| find contact (identifier / phone / email) | `GET /api/v1/accounts/{account_id}/contacts/search?q=` |
| create contact (+ contact inbox) | `POST /api/v1/accounts/{account_id}/contacts` (`inbox_id`, `name`, `phone_number`, `email`, `identifier`, `custom_attributes`, `additional_attributes`) |
| update reused contact | `PUT /api/v1/accounts/{account_id}/contacts/{id}` |
| contact details incl. contact inboxes | `GET /api/v1/accounts/{account_id}/contacts/{id}` |
| create contact inbox / source | `POST /api/v1/accounts/{account_id}/contacts/{id}/contact_inboxes` (`inbox_id`, `source_id`) |
| find existing archived conversations | `GET /api/v1/accounts/{account_id}/contacts/{id}/conversations` |
| create conversation | `POST /api/v1/accounts/{account_id}/conversations` (`inbox_id`, `contact_id`, `source_id`, `status`, `additional_attributes`) |
| conversation details | `GET /api/v1/accounts/{account_id}/conversations/{id}` |
| create message (text) | `POST …/conversations/{id}/messages` JSON (`content`, `message_type` incoming/outgoing, `private`, `content_type`, `content_attributes`, `external_created_at`, `source_id`) |
| create message with attachment | same endpoint, `multipart/form-data` with `attachments[]` |
| list messages (recovery / validation) | `GET …/conversations/{id}/messages?before=` |
| mark resolved | `POST …/conversations/{id}/toggle_status` (`status: resolved`) |
| custom attribute definition | `GET/POST /api/v1/accounts/{account_id}/custom_attribute_definitions` |

## Data mapping

| respond.io | Chatwoot |
|---|---|
| `FirstName` + `LastName` | contact `name` (falls back to phone, then `respond.io contact <id>`) |
| `PhoneNumber` (digits) | contact `phone_number` as E.164 (`+` prepended); invalid numbers are dropped and reported |
| `Email` | contact `email` (lower-cased) |
| `ContactID` | contact `identifier = respondio:<id>` **and** custom attribute `respondio_contact_id` |
| `Country`, `Lifecycle`, `Tags`, `Status`, `Assignee`, `Channels`, dates | contact `additional_attributes.country_code` and `additional_attributes.respondio.*` |
| `Conversation ID`, start/resolve times, assignee, category, closing note | conversation `additional_attributes.respondio_*` |
| `Message Type` incoming/outgoing | message `message_type` |
| `Content` (`text`) | message `content` |
| `Content` (`quick_reply`) | title + `[Quick replies: A \| B]` |
| `Content` (`whatsapp_template`) | rendered header/body/footer/buttons text; template name kept in `content_attributes.respondio.template` |
| `Content` (`attachment` / `sticker`) | downloaded and uploaded as `attachments[]` (image, video, audio, file) |
| `Content` (`location` / `contact`) | text with address + Google Maps link / contact card text |
| `Content` (`unsupported`) | placeholder text (respond.io did not export the content) |
| `Date & Time` (export timezone) | `external_created_at` (UTC ISO 8601) + `content_attributes.respondio.sent_at_*`; order preserved |
| `Message ID` (+ `Contact ID`) | message `source_id = respondio:<contact>:<message>` (used for recovery/idempotency) |
| `Sender Type`, `Sender ID`, `Channel ID`, `Sub Type` | `content_attributes.respondio.*` |

Messages are linked to conversations by contact id and the conversation time window
(the messages export carries no conversation id). Messages outside every window are attached to
the preceding conversation (or the first one); contacts that have messages but no conversation
row get one *synthetic* conversation.

## Limitations found (please read)

1. **Chatwoot cannot back-date messages.** `external_created_at` is accepted by the message
   builder but it is stored inside `content_attributes`; the visible `created_at` is the import
   time. Chronological order is preserved because messages are posted in order. If you want the
   original time visible in the thread, set `TIMESTAMP_PREFIX=true` (each message is prefixed with
   `🕓 2026-01-11 22:11`). This is a workaround, off by default.
2. Conversation `created_at` / `last_activity_at` are also the import time; archived
   conversations therefore appear as "recent" resolved conversations in the archive inbox.
3. **Outgoing messages are attributed to the API token's user** (the agent who owns the token).
   The original respond.io sender type (`n8n`, `broadcast`, `user`, …) is kept in
   `content_attributes.respondio.sender_type`.
4. `source_id` for the contact inbox: new contacts get the UUID Chatwoot generates; contacts that
   already exist (e.g. created by the live WhatsApp inbox) get a deterministic `respondio-<id>`.
5. Chatwoot allows at most 200 messages per conversation per minute (`Too many messages`); the
   tool throttles to `CONVERSATION_MESSAGES_PER_MINUTE` and retries after 61 s if it still hits it.
6. Incoming messages re-open resolved conversations, so conversations are created `open` and
   resolved only after the last message was imported.
7. `GET /contacts/{id}/conversations` returns only the 25 most recent conversations; recovery of
   lost local state is best-effort for contacts with more than 25 archived conversations
   (keep `output/migration_state.db`!).
8. Importing incoming messages fires the same events as live messages: automation rules and agent
   notifications may trigger. Disable automations for the archive inbox before the full run, and
   expect notification noise.
9. If the account has the `email_continuity_on_api_channel` feature enabled, Chatwoot would email
   outgoing messages to contacts that have an email address. The exported contacts have no
   emails, but check the setting if you have email-bearing contacts in Chatwoot.
10. Media URLs point at `cdn.chatapi.net`; if a link has expired the download fails after 4
    attempts and the message is recorded in `failed_media.csv` / `failed_messages.csv`. The
    conversation stops there (nothing skipped silently) unless `--placeholder-for-failed-media`.
11. The `Message ID` in the export is a second-precision timestamp; 23 ids are shared by two
    contacts. Messages are therefore keyed by `Contact ID + Message ID`.

## Rehearsing without touching the real account

```bash
python -m tests.fake_chatwoot 8765      # in one terminal
# in another terminal, with CHATWOOT_BASE_URL=http://127.0.0.1:8765, CHATWOOT_API_TOKEN=secret-token,
# CHATWOOT_ACCOUNT_ID=185855, CHATWOOT_ARCHIVE_INBOX_ID=145492 in a separate env file:
python migrate.py --check --env-file rehearsal.env
```

## Tests

```bash
python -m pytest -q
```

The suite spins up the fake Chatwoot server and covers: CSV detection, timezone conversion,
dedupe/linking, rendering of every payload type, fail-closed checks (WhatsApp inbox, wrong name,
locked inbox), dry-run write blocking, the test migration, idempotent re-runs, recovery without the
state DB, reuse of contacts created by the live WhatsApp inbox, failed media handling, the
per-conversation flood limit and the media test stage.
