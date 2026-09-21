#!/usr/bin/env python3
"""Turn captured messages into structured signals.

Per-channel regexes live in PATTERNS at the top of this file. Tune a group's
format there; the parsing logic below never needs to change.

Scoring uses the FIRST captured version of a message. Later revisions stay in
raw_messages.csv as evidence of tampering but do not replace the call as it was
originally posted -- otherwise a group could edit a signal until it parses (or
until it wins). Deleted messages are parsed and scored exactly like live ones;
their deletion is the whole point of the audit.
"""

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

RAW_CSV = Path("raw_messages.csv")
SIGNALS_CSV = Path("signals.csv")

csv.field_size_limit(10 * 1024 * 1024)

# --------------------------------------------------------------------------
# Per-channel regex patterns. Key = channel as written in channels.txt
# (matched case-insensitively, with or without the leading @).
# "__default__" is used for any channel without its own entry.
#
# Every pattern must expose the value as capture group 1. Patterns are applied
# with IGNORECASE | MULTILINE. Fields you omit are simply treated as absent.
# --------------------------------------------------------------------------
NUM = r"(\d[\d,]{0,6}(?:\.\d{1,5})?)"   # 5 dp: FX quotes like 1.21704

PATTERNS = {
    "__default__": {
        "direction": r"\b(buy|long|sell|short)\b",
        "symbol": r"\b(xau\s*/?\s*usd|xau|gold|gc\s*=\s*f|gc"
                  r"|(?:eur|usd|gbp|jpy|chf|aud|nzd|cad)\s*/?\s*"
                  r"(?:eur|usd|gbp|jpy|chf|aud|nzd|cad))\b",
        # A bare "@" used to be an entry cue, but it matches the "@" in
        # "SL @ 4410" just as happily, which silently files a stop loss as the
        # entry. Anchor on the words instead, and treat a direction word
        # followed by a price ("XAUUSD BUY 4435") as the entry it plainly is.
        "entry": r"(?:entry|enter|\b(?:buy|sell|long|short)\b)[^0-9\n]{0,12}" + NUM,
        "tp1": r"(?:tp\s*1|take[\s-]*profit[\s-]*1|target\s*1|\btp\b|\btarget\b)"
               r"[^0-9\n]{0,12}" + NUM,
        "tp2": r"(?:tp\s*2|take[\s-]*profit[\s-]*2|target\s*2)[^0-9\n]{0,12}" + NUM,
        "tp3": r"(?:tp\s*3|take[\s-]*profit[\s-]*3|target\s*3)[^0-9\n]{0,12}" + NUM,
        # "\bstop\b" must not swallow the order type in "XAUUSD BUY STOP 4341":
        # that number is the entry, and taking it as the stop loss puts the stop
        # exactly on the entry -- a zero-width stop that score.py then simulates
        # as an instant loss. The real "SL @ 4315" sits further along the line.
        "sl": r"(?:sl|s/l|stopp?[\s-]*loss|(?<!buy )(?<!sell )\bstopp?\b)"
              r"[^0-9\n]{0,12}" + NUM,
    },
    # ------------------------------------------------------------------
    # Example of a per-group override. Copy this block, rename the key to
    # the channel's @username and adjust only the fields that differ.
    #
    # "@example_signals": {
    #     "direction": r"\b(buy|sell)\b\s+gold",
    #     "symbol":    r"\b(gold)\b",
    #     "entry":     r"@\s*" + NUM,
    #     "tp1":       r"🎯\s*" + NUM,
    #     "tp2":       r"🎯\s*\d[\d,.]*\s*[\n/]\s*" + NUM,
    #     "tp3":       None,
    #     "sl":        r"🛑\s*" + NUM,
    # },
    # ------------------------------------------------------------------
}

# Directions collapse to two canonical values.
LONG_WORDS = {"buy", "long"}
SHORT_WORDS = {"sell", "short"}

# Everything this audit can price is gold; normalise the aliases so score.py
# has one symbol to map onto GC=F.
GOLD_ALIASES = {"xauusd", "xau/usd", "xau", "gold", "gc", "gc=f"}

# The currencies these channels actually quote. A pair is two of these, which
# is what separates a real symbol from any other six-letter run of capitals.
CURRENCIES = ("EUR", "USD", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD")

# A "price" outside its instrument's band is a parse artefact -- a date, a
# percentage, a pip count -- not a level. One global band cannot do this job:
# gold trades near 4400 and GBPCAD near 1.86, so a floor that keeps a pip count
# out of gold throws away every FX level ever posted.
GOLD_BAND = (500.0, 20000.0)
JPY_BAND = (50.0, 500.0)     # anything quoted in yen: USDJPY ~150, GBPJPY ~190
FX_BAND = (0.3, 5.0)         # every other pair quotes around 1


def band_for(symbol: str):
    """The plausible price range for a symbol, or None if it is not priceable."""
    if not symbol:
        return None
    if symbol == "XAUUSD":
        return GOLD_BAND
    if len(symbol) == 6 and symbol[:3] in CURRENCIES and symbol[3:] in CURRENCIES:
        return JPY_BAND if symbol[3:] == "JPY" else FX_BAND
    return None

# A row only becomes a signal if it has everything scoring requires.
REQUIRED_FIELDS = ("direction", "symbol", "tp1", "sl")

# Confidence weights; extra take-profits do not add confidence.
WEIGHTS = {"direction": 0.25, "symbol": 0.25, "tp1": 0.2, "sl": 0.2, "entry": 0.1}

OUT_COLUMNS = [
    "channel",
    "message_id",
    "timestamp_utc",
    "direction",
    "symbol",
    "entry",
    "tp1",
    "tp2",
    "tp3",
    "sl",
    "raw_text",
    "parse_confidence",
]


def patterns_for(channel: str) -> dict:
    key = channel.strip().lower().lstrip("@")
    for name, spec in PATTERNS.items():
        if name == "__default__":
            continue
        if name.strip().lower().lstrip("@") == key:
            return spec
    return PATTERNS["__default__"]


def search(pattern, text):
    if not pattern:
        return None
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return match.group(1) if match else None


def to_price(value, band):
    """A number is only a price if it falls in its own instrument's band."""
    if value is None or band is None:
        return None
    try:
        price = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    low, high = band
    if not (low <= price <= high):
        return None
    return price


def parse_message(channel: str, text: str):
    spec = patterns_for(channel)
    if not text or not text.strip():
        return None

    raw_direction = search(spec.get("direction"), text)
    direction = None
    if raw_direction:
        word = raw_direction.strip().lower()
        if word in LONG_WORDS:
            direction = "LONG"
        elif word in SHORT_WORDS:
            direction = "SHORT"

    raw_symbol = search(spec.get("symbol"), text)
    symbol = None
    if raw_symbol:
        # "EUR / USD", "eurusd" and "EUR/USD" are one symbol; squash everything
        # that is not a letter or digit before deciding.
        flat = re.sub(r"[^a-z0-9=]", "", raw_symbol.lower())
        symbol = "XAUUSD" if flat in GOLD_ALIASES else flat.upper()

    # The band depends on the symbol, so it has to be resolved first: 1.86 is a
    # real GBPCAD level and a parse artefact on gold, and only the symbol says
    # which this is.
    band = band_for(symbol)

    fields = {
        "direction": direction,
        "symbol": symbol,
        "entry": to_price(search(spec.get("entry"), text), band),
        "tp1": to_price(search(spec.get("tp1"), text), band),
        "tp2": to_price(search(spec.get("tp2"), text), band),
        "tp3": to_price(search(spec.get("tp3"), text), band),
        "sl": to_price(search(spec.get("sl"), text), band),
    }

    if any(fields[name] is None for name in REQUIRED_FIELDS):
        return None

    # A level set that contradicts its own direction is a mis-parse, not a
    # signal: a long's stop must sit below its target.
    if fields["direction"] == "LONG" and fields["sl"] >= fields["tp1"]:
        return None
    if fields["direction"] == "SHORT" and fields["sl"] <= fields["tp1"]:
        return None

    confidence = sum(w for name, w in WEIGHTS.items() if fields[name] is not None)
    fields["parse_confidence"] = round(confidence, 2)
    return fields


def main():
    if not RAW_CSV.exists():
        print(f"[info] {RAW_CSV} not found; nothing to parse")
        return 0

    with RAW_CSV.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    # First captured version of each message only.
    first_version = {}
    revisions = defaultdict(int)
    for row in rows:
        key = (row["channel"], row["message_id"])
        if key in first_version:
            revisions[row["channel"]] += 1
            continue
        first_version[key] = row

    signals = []
    seen = defaultdict(int)
    parsed = defaultdict(int)
    unparsed = defaultdict(list)

    for (channel, message_id), row in first_version.items():
        seen[channel] += 1
        fields = parse_message(channel, row.get("text", ""))
        if fields is None:
            unparsed[channel].append(message_id)
            continue
        parsed[channel] += 1
        signals.append(
            {
                "channel": channel,
                "message_id": message_id,
                "timestamp_utc": row["timestamp_utc"],
                "direction": fields["direction"],
                "symbol": fields["symbol"],
                "entry": "" if fields["entry"] is None else fields["entry"],
                "tp1": fields["tp1"],
                "tp2": "" if fields["tp2"] is None else fields["tp2"],
                "tp3": "" if fields["tp3"] is None else fields["tp3"],
                "sl": fields["sl"],
                "raw_text": row.get("text", ""),
                "parse_confidence": fields["parse_confidence"],
            }
        )

    signals.sort(key=lambda s: (s["channel"], s["timestamp_utc"], int(s["message_id"])))
    with SIGNALS_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(signals)

    print(f"\nwrote {len(signals)} signal(s) to {SIGNALS_CSV}\n")
    print(f"{'channel':<32} {'messages':>9} {'parsed':>7} {'unparsed':>9} {'edits':>6}")
    print("-" * 68)
    for channel in sorted(seen):
        n_unparsed = len(unparsed[channel])
        print(
            f"{channel:<32} {seen[channel]:>9} {parsed[channel]:>7} "
            f"{n_unparsed:>9} {revisions[channel]:>6}"
        )
    print(
        "\nUnparsed messages stay in raw_messages.csv only. A high unparsed "
        "count means that\nchannel needs its own entry in PATTERNS -- not that "
        "it posts few signals."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
