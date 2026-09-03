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
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

SIGNALS_CSV = Path("signals.csv")
SCORED_CSV = Path("scored.csv")

TICKER = "GC=F"
INTERVAL = "5m"
PERIOD = "60d"

COST_POINTS = 3.0          # round trip, charged to every closed trade
MAX_HOLD_HOURS = 48
DATA_HORIZON_WARN_DAYS = 50  # yfinance keeps ~60d of intraday data
DOWNLOAD_ATTEMPTS = 3

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
    "sl",
    "outcome",
    "exit_time_utc",
    "exit_price",
    "points_gross",
    "cost_points",
    "points_net",
    "bars_held",
    "same_candle_conflict",
    "scored_at_utc",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def load_scored_keys():
    if not SCORED_CSV.exists():
        return set()
    with SCORED_CSV.open("r", encoding="utf-8", newline="") as fh:
        return {(r["channel"], r["message_id"]) for r in csv.DictReader(fh)}


def walk(signal, times, o, h, l, c):
    """Walk the price path forward. Returns a result dict, or None when the
    trade cannot be resolved yet (data does not reach far enough)."""
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

    last_close, last_time, bars = float(c[i]), times[i], 0

    for j in range(i, len(times)):
        if times[j] > deadline:
            return {
                "outcome": "TIMEOUT",
                "entry_price": entry_price,
                "exit_price": last_close,
                "exit_time": last_time,
                "bars": bars,
                "conflict": False,
            }
        bars = j - i + 1
        last_close, last_time = float(c[j]), times[j]
        high, low = float(h[j]), float(l[j])

        tp_hit = high >= tp1 if is_long else low <= tp1
        sl_hit = low <= sl if is_long else high >= sl

        if tp_hit and sl_hit:
            # Both levels inside one candle: order unknowable -> call it a loss.
            return {
                "outcome": "LOSS",
                "entry_price": entry_price,
                "exit_price": sl,
                "exit_time": times[j],
                "bars": bars,
                "conflict": True,
            }
        if tp_hit:
            return {
                "outcome": "WIN",
                "entry_price": entry_price,
                "exit_price": tp1,
                "exit_time": times[j],
                "bars": bars,
                "conflict": False,
            }
        if sl_hit:
            return {
                "outcome": "LOSS",
                "entry_price": entry_price,
                "exit_price": sl,
                "exit_time": times[j],
                "bars": bars,
                "conflict": False,
            }

    # Ran out of candles before the 48h deadline: still open, score it later.
    return None


def main():
    if not SIGNALS_CSV.exists():
        print(f"[info] {SIGNALS_CSV} not found; run parse.py first")
        return 0

    with SIGNALS_CSV.open("r", encoding="utf-8", newline="") as fh:
        raw_signals = list(csv.DictReader(fh))
    if not raw_signals:
        print("[info] no signals to score")
        return 0

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
                "sl": float(row["sl"]),
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

        new_rows, unresolved, out_of_window = [], 0, 0
        scored_at = now_utc()

        for signal in sorted(pending, key=lambda s: s["ts"]):
            result = walk(signal, times, o, h, l, c)
            if result is None:
                unresolved += 1
                continue
            if result.get("unresolvable"):
                out_of_window += 1
                continue

            gross = (
                result["exit_price"] - result["entry_price"]
                if signal["direction"] == "LONG"
                else result["entry_price"] - result["exit_price"]
            )
            net = gross - COST_POINTS
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
                    "sl": signal["sl"],
                    "outcome": result["outcome"],
                    "exit_time_utc": result["exit_time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "exit_price": round(result["exit_price"], 3),
                    "points_gross": round(gross, 3),
                    "cost_points": COST_POINTS,
                    "points_net": round(net, 3),
                    "bars_held": result["bars"],
                    "same_candle_conflict": "yes" if result["conflict"] else "no",
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
