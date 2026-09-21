#!/usr/bin/env python3
"""PR gate: the parser must not lose signals, and its output must match its code.

Two failures this catches, both of which have actually happened here:

  * a pattern change that fixes one format and silently drops another. The
    count alone does not catch it -- six new FX signals can mask six lost gold
    ones -- so the check is per (channel, message_id), not a total.
  * signals.csv committed out of step with the parse.py that generates it.

Usage:  python check_parse.py <baseline-signals.csv>
"""

import csv
import subprocess
import sys
from pathlib import Path

SIGNALS = Path("signals.csv")
csv.field_size_limit(10 * 1024 * 1024)

KEYED_FIELDS = ("direction", "symbol", "entry", "tp1", "tp2", "tp3", "sl")


def load(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return {(r["channel"], r["message_id"]): r for r in csv.DictReader(fh)}


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: check_parse.py <baseline-signals.csv>")
    baseline_path = sys.argv[1]

    committed = SIGNALS.read_text(encoding="utf-8") if SIGNALS.exists() else ""

    print("[check] running parse.py")
    proc = subprocess.run([sys.executable, "parse.py"], capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        sys.exit("[FAIL] parse.py exited non-zero")

    regenerated = SIGNALS.read_text(encoding="utf-8")
    failures = []

    if committed and committed != regenerated:
        failures.append(
            "signals.csv does not match what parse.py produces. Regenerate it "
            "and commit it alongside the parser change."
        )
    else:
        print("[ok] signals.csv matches its parser")

    base = load(baseline_path)
    now = load(SIGNALS)
    lost = sorted(set(base) - set(now))
    gained = sorted(set(now) - set(base))

    if lost:
        failures.append(f"{len(lost)} signal(s) present on the base branch are gone")
        for key in lost[:20]:
            print(f"   LOST {key[0]} #{key[1]} {base[key].get('symbol','')}")
        if len(lost) > 20:
            print(f"   ... and {len(lost) - 20} more")
    else:
        print(f"[ok] no signals lost ({len(base)} on base, {len(now)} here)")

    # A silently rewritten level is as bad as a lost row, so compare the fields
    # that scoring depends on, for every signal both sides share.
    changed = [
        k for k in set(base) & set(now)
        if any(base[k].get(f) != now[k].get(f) for f in KEYED_FIELDS)
    ]
    if changed:
        print(f"[note] {len(changed)} existing signal(s) changed a parsed field:")
        for k in changed[:10]:
            diff = {f: (base[k].get(f), now[k].get(f))
                    for f in KEYED_FIELDS if base[k].get(f) != now[k].get(f)}
            print(f"   {k[0]} #{k[1]} {diff}")
        if len(changed) > 10:
            print(f"   ... and {len(changed) - 10} more")
        print("[note] intended when a parse bug is being fixed; review the diff above")

    if gained:
        print(f"[ok] {len(gained)} new signal(s)")

    if failures:
        print()
        for f in failures:
            print(f"[FAIL] {f}")
        return 1
    print("\n[PASS] parser check clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
