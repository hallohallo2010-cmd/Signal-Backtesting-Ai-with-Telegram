#!/usr/bin/env python3
"""Score parsed signals against 5-minute gold futures candles.

Rules, all deliberately unkind to the signal seller:

* TP1 and SL inside the SAME 5-minute candle counts as a LOSS. Intra-candle
  ordering is unknowable, so we take the conservative call every time.
* Every closed trade is charged COST_POINTS points of round-trip friction.
* Nothing is held longer than MAX_HOLD_HOURS; an unresolved trade closes flat
  at the last price and is still charged costs.
* Expectancy is always reported twice -- raw, and with each channel's best two
  trades removed. Two lucky calls should not carry a track record.

scored.csv is append-only: a signal already scored is never re-scored.
"""

import csv
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

import scoring

SIGNALS_CSV = Path("signals.csv")
SCORED_CSV = Path("scored.csv")

TICKER = "GC=F"

# Bump when the scoring model changes. Rows carrying a different version are
# re-simulated rather than trusted, which is what lets a model fix reach the
# trades already in the file instead of only the next ones.
SCORING_MODEL = "2-basis-dual"

# "XAUUSD BUY STOP 4341" is an order type, not a market entry.
PENDING_ORDER = re.compile(r"\b(buy|sell)[\s-]*(stop|limit)\b", re.I)
INTERVAL = "5m"
PERIOD = "60d"

COST_POINTS = 3.0          # round trip, charged to every closed trade
MAX_HOLD_HOURS = 48
DATA_HORIZON_WARN_DAYS = 50  # yfinance keeps ~60d of intraday data
DOWNLOAD_ATTEMPTS = 3

# Pip size per instrument, used only to record a second unit alongside points.
# These are the standard conventions. Note the channels themselves are not
# consistent about it: TNFX calls a 2.4 dollar gold move "+240 pips" (0.01),
# Forexman calls a 3 dollar move "+30 pips" (0.1). Their claims are therefore
# not comparable with each other, which is a reason to record our own unit
# rather than quote theirs.
PIP_SIZE_GOLD = 0.1
PIP_SIZE_JPY = 0.01
PIP_SIZE_FX = 0.0001

CURRENCIES = ("EUR", "USD", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD")


def pip_size(symbol: str):
    """Pip size for a symbol, or None if we have no convention for it."""
    symbol = (symbol or "").upper()
    if symbol in SYMBOL_MAP:
        return PIP_SIZE_GOLD
    if len(symbol) == 6 and symbol[:3] in CURRENCIES and symbol[3:] in CURRENCIES:
        return PIP_SIZE_JPY if symbol[3:] == "JPY" else PIP_SIZE_FX
    return None


# Symbols this audit can price. Everything else is left unscored on purpose.
SYMBOL_MAP = {
    "XAUUSD": TICKER,
    "XAU/USD": TICKER,
    "XAU": TICKER,
    "GOLD": TICKER,
    "GC": TICKER,
    "GC=F": TICKER,
}

csv.field_size_limit(10 * 1024 * 1024)

OUT_COLUMNS = [
    "channel",
    "message_id",
    "timestamp_utc",
    "direction",
    "symbol",
    "entry_stated",
    "entry_price",
    "tp1",
    "tp2",
    "tp3",
    "sl",
    "outcome",
    "exit_time_utc",
    "exit_price",
    "points_gross",
    "cost_points",
    "points_net",
    # Points are price units, which are not comparable across instruments: a
    # gold point is a dollar, a EURUSD point is ten thousand pips. Both of
    # these are recorded per trade from the outset because a trade cannot be
    # re-scored later -- yfinance serves roughly 60 days of intraday data, so
    # the candles behind an old trade are simply gone. Deriving these later
    # would be impossible, not merely expensive.
    "risk_points",      # |entry - sl| as filled: the risk actually taken
    "pips_net",         # points_net in pips, by the conventions above
    "r_multiple",       # points_net / risk_points: unit-free, cross-instrument
    # --- the headline model above is "what a follower got": the levels the
    # channel posted, restated in futures terms, entered at the market price
    # actually available. The columns below isolate the setup from the delay.
    "basis_points",     # futures-minus-spot premium applied to the levels
    "entry_mode",       # market | pending
    "slippage_points",  # fill minus the basis-adjusted stated entry
    "outcome_iso",      # same trade, levels re-anchored as distances from fill
    "points_net_iso",
    "r_multiple_iso",
    "model_version",
    "bars_held",
    "same_candle_conflict",
    # Counterfactual: did price reach TP2/TP3 before the stop or the deadline,
    # under a hold-through-TP1 rule? Context only -- expectancy still exits at
    # TP1. Empty when the signal never specified that level.
    "tp2_hit",
    "tp3_hit",
    "best_price_reached",
    "scored_at_utc",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def opt_float(value):
    """Optional numeric cell -> float or None. parse.py leaves absent levels blank."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def level_reached(level, best_price, is_long):
    """Did the favourable excursion reach this level? '' when undefined."""
    if level is None:
        return ""
    return "yes" if (best_price >= level if is_long else best_price <= level) else "no"


def load_prices() -> pd.DataFrame:
    last_error = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            df = yf.download(
                TICKER,
                period=PERIOD,
                interval=INTERVAL,
                auto_adjust=False,
                progress=False,
            )
        except Exception as exc:  # noqa: BLE001 - network flake, retry
            last_error = exc
            df = None
        if df is not None and not df.empty:
            break
        print(f"[warn] price download attempt {attempt} returned nothing")
    else:
        sys.exit(f"[fatal] could not download {TICKER} {INTERVAL} data: {last_error}")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df[["Open", "High", "Low", "Close"]].dropna()
    idx = pd.DatetimeIndex(df.index)
    df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df = df.sort_index()
    print(
        f"[info] {len(df)} {INTERVAL} candles for {TICKER} "
        f"from {df.index[0]} to {df.index[-1]} (UTC)"
    )
    return df


def drop_stale_rows():
    """Remove rows scored under an older model, so re-simulation replaces them
    instead of appending a second copy."""
    if not SCORED_CSV.exists():
        return
    with SCORED_CSV.open("r", encoding="utf-8", newline="") as fh:
        rows = [dict(r) for r in csv.DictReader(fh)]
    keep = [r for r in rows if r.get("model_version") == SCORING_MODEL]
    if len(keep) == len(rows):
        return
    with SCORED_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        for row in keep:
            writer.writerow({c: row.get(c, "") for c in OUT_COLUMNS})
    print(f"[info] dropped {len(rows) - len(keep)} row(s) for re-simulation")


def load_scored_keys():
    """Keys already scored. Also migrates a scored.csv written by an older
    schema: appending to a narrower header would misalign every new row, so the
    file is widened in place with the new cells left blank."""
    if not SCORED_CSV.exists():
        return set()
    with SCORED_CSV.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        rows = [dict(r) for r in reader]

    unknown = [c for c in header if c not in OUT_COLUMNS]
    if unknown:
        sys.exit(
            f"[fatal] {SCORED_CSV} has unrecognised column(s) {unknown}; "
            f"refusing to append to it"
        )
    if header and header != OUT_COLUMNS:
        with SCORED_CSV.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in OUT_COLUMNS})
        added = [c for c in OUT_COLUMNS if c not in header]
        print(f"[info] widened {SCORED_CSV} schema; new blank column(s): {added}")

    # Only rows scored under the current model count as done. A model change
    # therefore re-simulates the whole file, which is the point: a scoring fix
    # that only reached future trades would leave the old numbers standing.
    current = {
        (r["channel"], r["message_id"])
        for r in rows
        if r.get("model_version") == SCORING_MODEL
    }
    stale = len(rows) - len(current)
    if stale:
        print(
            f"[info] {stale} row(s) were scored under an older model and will "
            f"be re-simulated for {SCORING_MODEL}"
        )
    return current


def walk(signal, times, o, h, l, c):
    """Walk the price path forward from the signal timestamp.

    Returns a result dict, or None when the trade cannot be resolved yet
    (the price data does not reach far enough to settle it).

    The walk answers two separate questions in one pass:

    1. The trade itself: did TP1 or SL come first? That decides the outcome and
       is the only thing expectancy is built from.
    2. The counterfactual for TP2/TP3: how far did price run in the trade's
       favour before SL or the 48h deadline closed the position, *ignoring* the
       TP1 exit? That is a hold-through-TP1 variant, reported for context only.
       It never feeds expectancy -- the scored trade still exits at TP1.
    """
    ts = signal["ts"]
    if ts < times[0]:
        return {"unresolvable": "before_data_window"}

    i = int(times.searchsorted(ts, side="left"))
    if i >= len(times):
        return None  # posted after the last candle we have

    entry_price = float(o[i])
    deadline = ts + timedelta(hours=MAX_HOLD_HOURS)
    is_long = signal["direction"] == "LONG"
    tp1, sl = signal["tp1"], signal["sl"]

    resolution = None            # first TP1-or-SL event: the trade's outcome
    excursion = entry_price      # best price seen while the position could be open
    last_close, last_time, bars = float(c[i]), times[i], 0
    path_complete = False

    for j in range(i, len(times)):
        if times[j] > deadline:
            path_complete = True
            break
        bars = j - i + 1
        last_close, last_time = float(c[j]), times[j]
        high, low = float(h[j]), float(l[j])
        excursion = max(excursion, high) if is_long else min(excursion, low)

        tp_hit = high >= tp1 if is_long else low <= tp1
        sl_hit = low <= sl if is_long else high >= sl

        if resolution is None:
            if tp_hit and sl_hit:
                # Both levels inside one candle: order unknowable -> loss.
                resolution = ("LOSS", sl, times[j], bars, True)
            elif tp_hit:
                resolution = ("WIN", tp1, times[j], bars, False)
            elif sl_hit:
                resolution = ("LOSS", sl, times[j], bars, False)

        if sl_hit:
            # Position is out on the stop; the excursion stops here too.
            path_complete = True
            break

    if not path_complete:
        # Ran out of candles before the stop or the deadline. The trade may
        # already have touched TP1, but the TP2/TP3 path is still unfinished,
        # so leave it unscored and settle it on a later run.
        return None

    if resolution is None:
        outcome, exit_price, exit_time, conflict = "TIMEOUT", last_close, last_time, False
    else:
        outcome, exit_price, exit_time, bars, conflict = resolution

    return {
        "outcome": outcome,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "exit_time": exit_time,
        "bars": bars,
        "conflict": conflict,
        "excursion": excursion,
    }


def main():
    if not SIGNALS_CSV.exists():
        print(f"[info] {SIGNALS_CSV} not found; run parse.py first")
        return 0

    with SIGNALS_CSV.open("r", encoding="utf-8", newline="") as fh:
        raw_signals = list(csv.DictReader(fh))
    if not raw_signals:
        print("[info] no signals to score")
        return 0

    drop_stale_rows()
    already = load_scored_keys()
    pending, unsupported = [], []

    for row in raw_signals:
        key = (row["channel"], row["message_id"])
        if key in already:
            continue
        symbol = (row.get("symbol") or "").strip().upper()
        if SYMBOL_MAP.get(symbol) != TICKER:
            unsupported.append((row["channel"], row["message_id"], symbol))
            continue
        try:
            ts = pd.Timestamp(row["timestamp_utc"])
        except ValueError:
            print(f"[warn] {key}: unreadable timestamp {row['timestamp_utc']!r}")
            continue
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        pending.append(
            {
                "channel": row["channel"],
                "message_id": row["message_id"],
                "ts": ts,
                "direction": row["direction"],
                "symbol": symbol,
                "entry_stated": row.get("entry", ""),
                "tp1": float(row["tp1"]),
                "tp2": opt_float(row.get("tp2")),
                "tp3": opt_float(row.get("tp3")),
                "sl": float(row["sl"]),
                # A stop/limit order is not filled at the market price when it
                # is posted; it waits for its level. Detected from the text
                # because parse.py does not model order type.
                "pending": bool(PENDING_ORDER.search(row.get("raw_text") or "")),
            }
        )

    if unsupported:
        print(
            f"[info] {len(unsupported)} signal(s) skipped: symbol not priceable "
            f"with {TICKER} data"
        )

    # Loud, early warning: these may be past the intraday data horizon forever.
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=DATA_HORIZON_WARN_DAYS)
    stale = [s for s in pending if s["ts"] < cutoff]
    if stale:
        print("")
        print("!" * 78)
        print(
            f"!! WARNING: {len(stale)} UNSCORED SIGNAL(S) ARE OLDER THAN "
            f"{DATA_HORIZON_WARN_DAYS} DAYS."
        )
        print("!! yfinance keeps only ~60 days of intraday data. These signals are")
        print("!! about to become permanently unscoreable. Score them NOW or they")
        print("!! drop out of the sample -- which silently biases the track record.")
        for s in sorted(stale, key=lambda s: s["ts"])[:20]:
            age = (pd.Timestamp.now(tz="UTC") - s["ts"]).days
            print(f"!!   {s['channel']} #{s['message_id']} {s['ts']} ({age}d old)")
        if len(stale) > 20:
            print(f"!!   ... and {len(stale) - 20} more")
        print("!" * 78)
        print("")

    if not pending:
        print("[info] nothing new to score")
    else:
        df = load_prices()
        times = df.index
        o, h, l, c = (df[col].to_numpy() for col in ("Open", "High", "Low", "Close"))

        rolls = scoring.contract_roll_dates(times[0], times[-1])
        print(f"[info] contract roll boundaries in window: "
              f"{[str(t.date()) for t in rolls] or 'none'}")

        # The fill is the market open at the signal's timestamp. It does not
        # depend on the basis, so it can be resolved first and then used to
        # measure the basis.
        for signal in pending:
            idx = int(times.searchsorted(signal["ts"], side="left"))
            signal["fill_idx"] = idx if idx < len(times) else None
            signal["fill"] = float(o[idx]) if idx < len(times) else None

        observations = [
            (s["ts"], s["fill"] - float(s["entry_stated"]))
            for s in pending
            if s["fill"] is not None and str(s.get("entry_stated") or "").strip()
        ]
        print(f"[info] basis estimated from {len(observations)} signal(s) with a "
              f"stated entry, {scoring.BASIS_WINDOW_DAYS}-day rolling median")

        # The channel's next signal supersedes an unfilled pending order.
        next_signal_at = {}
        by_channel = {}
        for s in sorted(pending, key=lambda s: s["ts"]):
            by_channel.setdefault(s["channel"], []).append(s)
        for chan, items in by_channel.items():
            for a, b in zip(items, items[1:]):
                next_signal_at[(chan, a["message_id"])] = b["ts"]

        new_rows, unresolved, out_of_window = [], 0, 0
        missed, expired, conflicts = 0, 0, 0
        scored_at = now_utc()

        for signal in sorted(pending, key=lambda s: s["ts"]):
            is_long = signal["direction"] == "LONG"

            basis = scoring.basis_at(observations, signal["ts"], rolls)
            if basis is None or signal["fill"] is None:
                unresolved += 1
                continue

            # Headline: the channel's own levels, restated in futures terms.
            adj = scoring.adjust_levels(signal, basis)
            head = dict(signal, tp1=adj["tp1"], tp2=adj["tp2"],
                        tp3=adj["tp3"], sl=adj["sl"])

            entry_mode = "pending" if signal.get("pending") else "market"
            if entry_mode == "pending" and adj["entry"] is not None:
                # Wait for the level to trade. Expire at the channel's next
                # signal or PENDING_EXPIRY_HOURS, whichever comes first.
                deadline = signal["ts"] + timedelta(hours=scoring.PENDING_EXPIRY_HOURS)
                successor = next_signal_at.get((signal["channel"], signal["message_id"]))
                if successor is not None:
                    deadline = min(deadline, successor)
                touch = None
                for j in range(signal["fill_idx"], len(times)):
                    if times[j] > deadline:
                        break
                    if float(l[j]) <= adj["entry"] <= float(h[j]):
                        touch = j
                        break
                if touch is None:
                    expired += 1
                    continue
                head = dict(head, ts=times[touch])

            result = walk(head, times, o, h, l, c)
            if result is None:
                unresolved += 1
                continue
            if result.get("unresolvable"):
                out_of_window += 1
                continue

            # A fill already beyond the trade's own target or stop is not a
            # trade anyone could take; booking it as a win or a loss would be
            # an invention. Counted and excluded.
            if scoring.entry_already_past(adj, result["entry_price"], is_long):
                missed += 1
                continue
            if result["conflict"]:
                conflicts += 1
            gross = (
                result["exit_price"] - result["entry_price"]
                if is_long
                else result["entry_price"] - result["exit_price"]
            )
            net = gross - COST_POINTS

            # Risk as actually taken: the simulated fill against the stop, not
            # the stated entry, which the channel may never have got.
            risk = abs(result["entry_price"] - signal["sl"])
            if risk <= 0:
                # A stop sitting on the fill leaves no risk to divide by. It
                # should not happen now the BUY STOP mis-parse is fixed, but a
                # silent inf in an evidence file would be worse than a blank.
                risk, r_mult = None, None
            else:
                r_mult = net / risk
            size = pip_size(signal["symbol"])
            pips = None if size is None else net / size

            # Isolation mode: the same setup re-anchored on the fill, which
            # hands the posting delay back to the channel. Reported beside the
            # headline, never instead of it -- the gap between the two is what
            # this channel costs a follower by posting late.
            # Anchored on `head`, not `signal`: a pending order's headline walk
            # starts at the touch, and the isolation walk has to start from the
            # same fill or the two modes are measuring different trades.
            iso_levels = scoring.distance_levels(signal, result["entry_price"])
            outcome_iso = points_iso = r_iso = None
            if iso_levels is not None:
                iso_sig = dict(head, tp1=iso_levels["tp1"], tp2=iso_levels["tp2"],
                               tp3=iso_levels["tp3"], sl=iso_levels["sl"])
                iso_res = walk(iso_sig, times, o, h, l, c)
                if iso_res and not iso_res.get("unresolvable"):
                    iso_gross = (
                        iso_res["exit_price"] - iso_res["entry_price"] if is_long
                        else iso_res["entry_price"] - iso_res["exit_price"]
                    )
                    points_iso = iso_gross - COST_POINTS
                    outcome_iso = iso_res["outcome"]
                    iso_risk = abs(iso_res["entry_price"] - iso_levels["sl"])
                    r_iso = points_iso / iso_risk if iso_risk > 0 else None

            slippage = (
                None if adj["entry"] is None
                else result["entry_price"] - adj["entry"]
            )

            new_rows.append(
                {
                    "channel": signal["channel"],
                    "message_id": signal["message_id"],
                    "timestamp_utc": signal["ts"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "direction": signal["direction"],
                    "symbol": signal["symbol"],
                    "entry_stated": signal["entry_stated"],
                    "entry_price": round(result["entry_price"], 3),
                    "tp1": signal["tp1"],
                    "tp2": "" if signal["tp2"] is None else signal["tp2"],
                    "tp3": "" if signal["tp3"] is None else signal["tp3"],
                    "sl": signal["sl"],
                    "outcome": result["outcome"],
                    "exit_time_utc": result["exit_time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "exit_price": round(result["exit_price"], 3),
                    "points_gross": round(gross, 3),
                    "cost_points": COST_POINTS,
                    "points_net": round(net, 3),
                    "risk_points": "" if risk is None else round(risk, 5),
                    "pips_net": "" if pips is None else round(pips, 1),
                    "r_multiple": "" if r_mult is None else round(r_mult, 3),
                    "basis_points": round(basis, 2),
                    "entry_mode": entry_mode,
                    "slippage_points": "" if slippage is None else round(slippage, 2),
                    "outcome_iso": outcome_iso or "",
                    "points_net_iso": "" if points_iso is None else round(points_iso, 3),
                    "r_multiple_iso": "" if r_iso is None else round(r_iso, 3),
                    "model_version": SCORING_MODEL,
                    "bars_held": result["bars"],
                    "same_candle_conflict": "yes" if result["conflict"] else "no",
                    "tp2_hit": level_reached(
                        signal["tp2"], result["excursion"], is_long
                    ),
                    "tp3_hit": level_reached(
                        signal["tp3"], result["excursion"], is_long
                    ),
                    "best_price_reached": round(result["excursion"], 3),
                    "scored_at_utc": scored_at,
                }
            )

        if new_rows:
            exists = SCORED_CSV.exists()
            with SCORED_CSV.open("a", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
                if not exists:
                    writer.writeheader()
                writer.writerows(new_rows)
        if missed:
            print(f"[info] {missed} signal(s) MISSED: the fill was already beyond "
                  f"the trade's own target or stop; excluded from expectancy")
        if expired:
            print(f"[info] {expired} pending order(s) EXPIRED unfilled")
        print(f"[info] {conflicts} trade(s) had TP and SL inside one candle; "
              f"resolved as SL first")
        print(
            f"[info] scored {len(new_rows)} new signal(s); {unresolved} still open "
            f"(not enough price data yet); {out_of_window} before the data window"
        )

    report()
    return 0


def report():
    if not SCORED_CSV.exists():
        print("[info] nothing scored yet")
        return

    df = pd.read_csv(SCORED_CSV)
    if df.empty:
        print("[info] scored.csv is empty")
        return

    print("\n" + "=" * 78)
    print(f"RESULTS  (costs {COST_POINTS} pts/round trip, max hold {MAX_HOLD_HOURS}h,")
    print(" TP1+SL in the same candle scored as a LOSS)")
    print("=" * 78)

    header = (
        f"{'channel':<26} {'n':>4} {'win%':>6} {'exp':>9} "
        f"{'exp-best2':>10} {'W/L/TO':>12}"
    )
    print(header)
    print("-" * len(header))

    for channel, group in df.groupby("channel", sort=True):
        n = len(group)
        pts = group["points_net"].sort_values(ascending=False)
        wins = int((group["outcome"] == "WIN").sum())
        losses = int((group["outcome"] == "LOSS").sum())
        timeouts = int((group["outcome"] == "TIMEOUT").sum())
        win_rate = 100.0 * wins / n
        expectancy = pts.mean()
        trimmed = pts.iloc[2:]
        trimmed_exp = trimmed.mean() if len(trimmed) else float("nan")
        trimmed_str = "n/a" if trimmed.empty else f"{trimmed_exp:+.2f}"
        print(
            f"{channel:<26} {n:>4} {win_rate:>5.1f}% {expectancy:>+8.2f} "
            f"{trimmed_str:>10} {f'{wins}/{losses}/{timeouts}':>12}"
        )
        if n < 50:
            print(f"{'':<26} VERDICT WITHHELD - n={n} is below the n>=50 threshold")

    conflicts = int((df["same_candle_conflict"] == "yes").sum())
    if conflicts:
        print(
            f"\n[note] {conflicts} trade(s) had TP1 and SL inside one candle and "
            f"were scored as losses."
        )
    print(
        "\n'exp' is mean net points per trade. 'exp-best2' is the same figure with "
        "each\nchannel's two best trades removed. Report both, always."
    )


if __name__ == "__main__":
    sys.exit(main())
