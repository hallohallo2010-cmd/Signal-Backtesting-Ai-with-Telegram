#!/usr/bin/env python3
"""Append-only capture of Telegram signal-channel messages.

Design rule that everything else follows from: raw_messages.csv is EVIDENCE,
not a working set. Rows are only ever appended. The single permitted mutation
of an existing row is stamping deleted_detected_at, because a deletion is
itself a finding and there is nowhere else to record it.

Auth comes from the environment (GitHub Actions secrets). Secret values are
never printed, logged, or written to disk.
"""

import csv
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

CHANNELS_FILE = Path(os.environ.get("CHANNELS_FILE", "channels.txt"))
RAW_CSV = Path(os.environ.get("RAW_CSV", "raw_messages.csv"))

COLUMNS = [
    "channel",
    "message_id",
    "timestamp_utc",
    "text",
    "captured_at_utc",
    "edited_at",
    "deleted_detected_at",
]

FETCH_LIMIT = 100
SLEEP_BETWEEN_CHANNELS = 2.0
# csv.field_size_limit default is fine for messages, but long forwarded posts
# can be large; raise it so a single fat row can never abort a run.
csv.field_size_limit(10 * 1024 * 1024)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(dt) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_text(text: str) -> str:
    """Stable representation so a CSV round trip never looks like an edit."""
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_channels(path: Path):
    if not path.exists():
        print(f"[warn] {path} not found; nothing to capture")
        return []
    channels = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            channels.append(line)
    return channels


def load_rows(path: Path):
    """Return existing rows in file order. Abort on a schema mismatch rather
    than risk writing a misaligned row into the evidence file."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return []
        if reader.fieldnames != COLUMNS:
            sys.exit(
                f"[fatal] {path} header {reader.fieldnames} does not match the "
                f"expected schema {COLUMNS}; refusing to touch it"
            )
        return [dict(row) for row in reader]


def write_all(path: Path, rows):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def append_rows(path: Path, rows):
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def index_rows(rows):
    """(channel, message_id) -> list of rows, in capture order."""
    index = {}
    for row in rows:
        try:
            mid = int(row["message_id"])
        except (TypeError, ValueError):
            continue
        index.setdefault((row["channel"], mid), []).append(row)
    return index


def build_client():
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    session = os.environ.get("TG_SESSION", "").strip()

    missing = [
        name
        for name, value in (
            ("TG_API_ID", api_id),
            ("TG_API_HASH", api_hash),
            ("TG_SESSION", session),
        )
        if not value
    ]
    if missing:
        # Names only. Never the values.
        sys.exit(f"[fatal] missing required secrets: {', '.join(missing)}")
    if not api_id.isdigit():
        sys.exit("[fatal] TG_API_ID must be numeric")

    return TelegramClient(StringSession(session), int(api_id), api_hash)


def capture_channel(client, channel, index, new_rows, captured_at):
    """Returns (n_new, n_edits, n_deletions) or raises."""
    messages = [m for m in client.get_messages(channel, limit=FETCH_LIMIT) if m is not None]
    if not messages:
        # An empty fetch is indistinguishable from a permissions/network
        # oddity, so it must never be read as "everything was deleted".
        print(f"[warn] {channel}: no messages returned; skipping deletion check")
        return 0, 0, 0

    fetched = {m.id: m for m in messages}
    min_id, max_id = min(fetched), max(fetched)

    n_new = n_edits = n_deleted = 0

    for mid in sorted(fetched):
        msg = fetched[mid]
        text = normalize_text(msg.message or "")
        prior = index.get((channel, mid))

        if not prior:
            row = {
                "channel": channel,
                "message_id": str(mid),
                "timestamp_utc": iso(msg.date),
                "text": text,
                "captured_at_utc": captured_at,
                # If the post was already edited before we first saw it, that
                # is worth recording on the very first row.
                "edited_at": iso(getattr(msg, "edit_date", None)),
                "deleted_detected_at": "",
            }
            new_rows.append(row)
            index.setdefault((channel, mid), []).append(row)
            n_new += 1
            continue

        if normalize_text(prior[-1]["text"]) != text:
            # An edit never overwrites history: it appends a new revision row.
            row = {
                "channel": channel,
                "message_id": str(mid),
                "timestamp_utc": iso(msg.date),
                "text": text,
                "captured_at_utc": captured_at,
                "edited_at": iso(getattr(msg, "edit_date", None)) or captured_at,
                "deleted_detected_at": "",
            }
            new_rows.append(row)
            prior.append(row)
            n_edits += 1

    # Deletion detection is only valid inside the ID range we actually fetched.
    # Anything older than min_id simply fell out of the 100-message window and
    # its absence proves nothing.
    for (chan, mid), rows in index.items():
        if chan != channel or mid in fetched:
            continue
        if not (min_id <= mid <= max_id):
            continue
        if any(r["deleted_detected_at"] for r in rows):
            continue
        for r in rows:
            r["deleted_detected_at"] = captured_at
        n_deleted += 1
        print(f"[DELETION] {channel} message_id={mid} no longer served by the API")

    return n_new, n_edits, n_deleted


def main():
    channels = read_channels(CHANNELS_FILE)
    if not channels:
        print("[info] no channels configured; nothing to do")
        return 0

    existing = load_rows(RAW_CSV)
    index = index_rows(existing)
    print(f"[info] loaded {len(existing)} existing rows covering {len(index)} message ids")

    new_rows = []
    deletions_total = 0
    ok = 0
    failed = []

    with build_client() as client:
        if not client.is_user_authorized():
            sys.exit("[fatal] TG_SESSION is not authorized; regenerate the StringSession")

        for i, channel in enumerate(channels):
            captured_at = now_utc()
            try:
                n_new, n_edits, n_del = capture_channel(
                    client, channel, index, new_rows, captured_at
                )
            except FloodWaitError as exc:
                # Do not sleep out a long flood wait inside a 20-minute cron;
                # the next scheduled run will pick the channel back up.
                print(f"[error] {channel}: flood wait {exc.seconds}s, skipping this run")
                failed.append(channel)
            except Exception as exc:  # noqa: BLE001 - one bad channel must not sink the run
                print(f"[error] {channel}: {type(exc).__name__}: {exc}")
                failed.append(channel)
            else:
                ok += 1
                deletions_total += n_del
                print(
                    f"[ok] {channel}: new={n_new} edits={n_edits} deletions={n_del}"
                )
            if i + 1 < len(channels):
                time.sleep(SLEEP_BETWEEN_CHANNELS)

    if deletions_total:
        # A deletion stamp is the only in-place change we ever make, and it
        # forces a full rewrite; every other row is copied through untouched.
        write_all(RAW_CSV, existing + new_rows)
        print(f"[info] rewrote {RAW_CSV} to stamp {deletions_total} deletion(s)")
    elif new_rows:
        append_rows(RAW_CSV, new_rows)
        print(f"[info] appended {len(new_rows)} row(s) to {RAW_CSV}")
    else:
        print("[info] no changes")

    if failed and ok == 0:
        print(f"[fatal] every channel failed: {', '.join(failed)}")
        return 1
    if failed:
        print(f"[warn] {len(failed)} channel(s) failed this run: {', '.join(failed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
