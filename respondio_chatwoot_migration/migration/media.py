"""Download respond.io media to local disk before uploading it to Chatwoot."""

from __future__ import annotations

import csv
import logging
import mimetypes
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from .models import MediaRef, MigrationPlan, SourceMessage
from .state import StateDB

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
PERMANENT_STATUSES = {400, 401, 403, 404, 410}


@dataclass
class MediaResult:
    ok: bool
    path: Optional[Path]
    nbytes: int
    attempts: int
    http_status: Optional[int]
    error: str
    cached: bool = False


def guess_extension(media: MediaRef) -> str:
    ext = (media.ext or "").strip().lstrip(".").lower()
    if ext:
        return ext
    if media.file_name and "." in media.file_name:
        return media.file_name.rsplit(".", 1)[-1].lower()
    mime = (media.mime or "").split(";")[0].strip()
    guessed = mimetypes.guess_extension(mime) if mime else None
    if guessed:
        return guessed.lstrip(".")
    tail = media.url.split("?")[0].rsplit("/", 1)[-1]
    if "." in tail:
        return tail.rsplit(".", 1)[-1].lower()
    return "bin"


def local_name(message_key: str, media: MediaRef) -> str:
    base = media.file_name or media.url.split("?")[0].rsplit("/", 1)[-1] or "file"
    base = _SAFE.sub("_", base).strip("._") or "file"
    ext = guess_extension(media)
    if not base.lower().endswith("." + ext):
        base = f"{base}.{ext}"
    return f"{message_key.replace(':', '_')}_{base}"


class MediaDownloader:
    def __init__(
        self,
        media_dir: Path,
        state: Optional[StateDB],
        logger: Optional[logging.Logger] = None,
        timeout: int = 120,
        max_attempts: int = 4,
        session: Optional[requests.Session] = None,
    ):
        self.media_dir = Path(media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.state = state
        self.log = logger or logging.getLogger("migration")
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "respondio-chatwoot-migration/0.1"})

    def path_for(self, msg: SourceMessage) -> Path:
        assert msg.media is not None
        return self.media_dir / local_name(msg.key, msg.media)

    def download(self, msg: SourceMessage) -> MediaResult:
        assert msg.media is not None
        media = msg.media
        message_id = msg.key
        target = self.path_for(msg)
        if target.exists() and target.stat().st_size > 0:
            size = target.stat().st_size
            if media.size is None or media.size == size:
                self._record(message_id, media, target, size, "downloaded", 200, 0, None)
                return MediaResult(True, target, size, 0, 200, "", cached=True)
            self.log.warning("Local file %s has %d bytes but export says %s; re-downloading", target.name, size, media.size)

        tmp = target.with_suffix(target.suffix + ".part")
        attempts = 0
        last_error = ""
        last_status: Optional[int] = None
        while attempts < self.max_attempts:
            attempts += 1
            try:
                with self.session.get(media.url, stream=True, timeout=self.timeout) as resp:
                    last_status = resp.status_code
                    if resp.status_code >= 400:
                        last_error = f"HTTP {resp.status_code}"
                        if resp.status_code in PERMANENT_STATUSES:
                            self.log.error("Media %s for message %s is expired or inaccessible (%s)", media.url, message_id, last_error)
                            break
                        raise requests.HTTPError(last_error)
                    with tmp.open("wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1 << 16):
                            if chunk:
                                fh.write(chunk)
                size = tmp.stat().st_size
                if size == 0:
                    raise requests.HTTPError("empty response body")
                tmp.replace(target)
                self._record(message_id, media, target, size, "downloaded", last_status, attempts, None)
                return MediaResult(True, target, size, attempts, last_status, "")
            except (requests.RequestException, OSError) as exc:
                last_error = str(exc) or last_error or exc.__class__.__name__
                if attempts < self.max_attempts:
                    delay = min(30.0, 2 ** attempts + random.uniform(0, 1))
                    self.log.warning("Download %s failed (%s); retry %d in %.1fs", media.url, last_error, attempts, delay)
                    time.sleep(delay)
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        self.log.error("Media download FAILED for message %s: %s (%s)", message_id, media.url, last_error)
        self._record(message_id, media, None, None, "failed", last_status, attempts, last_error)
        return MediaResult(False, None, 0, attempts, last_status, last_error)

    def _record(self, message_key, media, path, size, status, http_status, attempts, error) -> None:
        if self.state is None:
            return
        self.state.save_media(
            message_key,
            message_key.split(":", 1)[-1],
            media.url,
            media.kind,
            media.mime,
            str(path) if path else None,
            size,
            status,
            http_status,
            attempts,
            error,
        )


def pick_media_sample(plan: MigrationPlan, n: int = 10) -> list[SourceMessage]:
    """Choose ~n media messages covering images, audio, video and documents."""
    by_kind: dict[str, list[SourceMessage]] = {"image": [], "audio": [], "video": [], "file": [], "sticker": []}
    for _, _, msg in plan.iter_messages():
        if msg.media and msg.media.kind in by_kind:
            by_kind[msg.media.kind].append(msg)
    quota = {"image": 2, "audio": 1, "video": 1, "file": 1}
    chosen: list[SourceMessage] = []
    for kind, q in quota.items():
        chosen.extend(by_kind[kind][:q])
    pool = [m for kind in ("image", "video", "file", "audio", "sticker") for m in by_kind[kind] if m not in chosen]
    for m in pool:
        if len(chosen) >= n:
            break
        chosen.append(m)
    return chosen[:n]


def run_media_test(plan: MigrationPlan, downloader: MediaDownloader, report_path: Path, n: int = 10, logger=None) -> list[dict]:
    log = logger or logging.getLogger("migration")
    sample = pick_media_sample(plan, n)
    rows = []
    for msg in sample:
        assert msg.media is not None
        res = downloader.download(msg)
        rows.append(
            {
                "message_id": msg.message_id,
                "contact_id": msg.contact_id,
                "kind": msg.media.kind,
                "mime": msg.media.mime,
                "file_name": msg.media.file_name,
                "url": msg.media.url,
                "status": "success" if res.ok else "failed",
                "cached": res.cached,
                "bytes": res.nbytes,
                "expected_bytes": msg.media.size or "",
                "attempts": res.attempts,
                "http_status": res.http_status or "",
                "local_path": str(res.path) if res.path else "",
                "error": res.error,
            }
        )
        log.info("media test %s %s -> %s %s", msg.media.kind, msg.media.file_name or msg.media.url, "OK" if res.ok else "FAILED", res.error)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["message_id"])
        writer.writeheader()
        writer.writerows(rows)
    ok = sum(1 for r in rows if r["status"] == "success")
    log.info("Media test: %d/%d downloads succeeded. Report: %s", ok, len(rows), report_path)
    return rows
