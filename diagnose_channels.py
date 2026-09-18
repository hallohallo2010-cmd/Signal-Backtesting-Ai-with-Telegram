#!/usr/bin/env python3
"""Report what each channels.txt entry actually resolves to.

capture.py trusts an @username to be the channel you think it is. Telegram
does not guarantee that: a username can belong to a person (so it resolves to
your private chat with them), can have been released and re-registered by
someone else, or can simply not exist while you still read the real channel
through a private invite link.

Nothing here writes to the evidence file. It only prints.

Run it from the capture workflow so it sees the real TG_SESSION.
"""

import difflib
import os
import re
import sys
from pathlib import Path

from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, User

CHANNELS_FILE = Path(os.environ.get("CHANNELS_FILE", "channels.txt"))


def read_channels(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def build_client():
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    session = os.environ.get("TG_SESSION", "").strip()
    missing = [
        n
        for n, v in (("TG_API_ID", api_id), ("TG_API_HASH", api_hash), ("TG_SESSION", session))
        if not v
    ]
    if missing:
        sys.exit(f"[fatal] missing required secrets: {', '.join(missing)}")
    return TelegramClient(StringSession(session), int(api_id), api_hash)


def squash(text: str) -> str:
    """Lowercase alphanumerics only, so '@TopNotchForex' and 'Top Notch Forex'
    compare equal."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def rank_candidates(wanted: str, dialogs, limit=3):
    """Best-matching joined channels for a name that did not resolve.

    Scores against both title and username: a channel's public username often
    looks nothing like its display name, and the reverse is just as common.
    """
    target = squash(wanted)
    scored = []
    for eid, title, username in dialogs:
        best = 0.0
        for field in (title, username):
            cand = squash(field)
            if not cand:
                continue
            ratio = difflib.SequenceMatcher(None, target, cand).ratio()
            if target and (target in cand or cand in target):
                ratio = max(ratio, 0.9)
            best = max(best, ratio)
        scored.append((best, eid, title, username))
    scored.sort(reverse=True)
    return [s for s in scored[:limit] if s[0] > 0.3]


def describe(entity):
    """(kind, id, title, username) for any entity Telethon hands back."""
    if isinstance(entity, User):
        name = " ".join(filter(None, [entity.first_name, entity.last_name])) or "(no name)"
        kind = "BOT" if entity.bot else "USER (private chat)"
        return kind, entity.id, name, entity.username
    if isinstance(entity, Chat):
        return "GROUP (basic)", entity.id, entity.title, None
    if isinstance(entity, Channel):
        kind = "SUPERGROUP" if entity.megagroup else "CHANNEL (broadcast)"
        return kind, entity.id, entity.title, entity.username
    return type(entity).__name__, getattr(entity, "id", "?"), "?", None


def last_message_date(client, entity):
    try:
        msgs = client.get_messages(entity, limit=1)
    except Exception as exc:  # noqa: BLE001
        return f"(could not read: {type(exc).__name__})"
    if not msgs:
        return "(no messages)"
    top = msgs[0]
    return f"{top.date:%Y-%m-%d %H:%M UTC} (id {top.id})"


def main():
    configured = read_channels(CHANNELS_FILE)

    with build_client() as client:
        if not client.is_user_authorized():
            sys.exit("[fatal] TG_SESSION is not authorized; regenerate the StringSession")

        print("=" * 78)
        print("PART 1 - what each channels.txt entry resolves to")
        print("=" * 78)
        wrong_kind = []
        unresolved = []
        broken = []
        for name in configured:
            try:
                entity = client.get_entity(name)
            except Exception as exc:  # noqa: BLE001
                print(f"\n{name}\n    UNRESOLVED: {type(exc).__name__}: {exc}")
                unresolved.append(name)
                broken.append(name)
                continue
            kind, eid, title, username = describe(entity)
            print(f"\n{name}")
            print(f"    resolves to : {kind}")
            print(f"    id          : {eid}")
            print(f"    title       : {title}")
            print(f"    username    : @{username}" if username else "    username    : (none)")
            print(f"    last message: {last_message_date(client, entity)}")
            if not (isinstance(entity, Channel) and entity.broadcast):
                print("    ^^ NOT a broadcast channel - captured rows from this are not signals")
                wrong_kind.append(name)
                broken.append(name)

        print()
        print("=" * 78)
        print("PART 2 - every broadcast channel this account can actually read")
        print("=" * 78)
        print("Match these against what you see in the Telegram app, then put the")
        print("id (the number) into channels.txt. An id can never silently resolve")
        print("to the wrong chat the way a username can.")
        print()

        rows = []
        for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not (isinstance(entity, Channel) and entity.broadcast):
                continue
            kind, eid, title, username = describe(entity)
            rows.append((dialog.date, eid, title, username))

        rows.sort(key=lambda r: (r[0] is None, r[0]), reverse=True)
        print(f"{'id':>16}  {'username':<26} {'last activity':<18} title")
        print("-" * 78)
        for date, eid, title, username in rows:
            uname = f"@{username}" if username else "(private)"
            when = f"{date:%Y-%m-%d %H:%M}" if date else "?"
            print(f"{eid:>16}  {uname:<26} {when:<18} {title}")

        if broken:
            dialogs = [(eid, title, username) for _, eid, title, username in rows]
            print()
            print("=" * 78)
            print("PART 3 - suggested channels.txt lines for the broken entries")
            print("=" * 78)
            print("Best title/username matches among the channels you have joined.")
            print("CHECK each one against the subscriber count and title in your app")
            print("before pasting - this is a string match, not a confirmation.")
            print()
            for name in broken:
                print(f"{name}:")
                candidates = rank_candidates(name, dialogs)
                if not candidates:
                    print("    no similar joined channel found - find it in the app,")
                    print("    then take its id from PART 2")
                for score, eid, title, username in candidates:
                    uname = f"@{username}" if username else "(private)"
                    print(f"    {eid}={name}".ljust(44) + f"# {title}  {uname}  [match {score:.0%}]")
                print()

        print()
        print("=" * 78)
        print("SUMMARY")
        print("=" * 78)
        print(f"channels.txt entries      : {len(configured)}")
        print(f"unresolved                : {len(unresolved)}  {unresolved or ''}")
        print(f"resolved to the wrong kind: {len(wrong_kind)}  {wrong_kind or ''}")
        print(f"broadcast channels joined : {len(rows)}")
        if unresolved or wrong_kind:
            print()
            print("Each name above that is unresolved or the wrong kind is a channel")
            print("the audit has never actually read. Replace it with an id from PART 2.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
