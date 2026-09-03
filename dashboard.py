#!/usr/bin/env python3
"""Render report.html from the audit's CSVs.

Self-contained output: one HTML file, no external assets, no CDN, no fonts to
fetch. It opens from the filesystem and works offline, including in Safari on
iOS. Everything interactive is inline JavaScript over data embedded in the page.

Reads (all optional -- a missing file degrades the report, never crashes it):
  scored.csv        settled trades: the only source of expectancy
  signals.csv       parsed signals: the "total signals" denominator
  raw_messages.csv  the evidence log: deletions and edits

Reads exactly one environment variable, POSITION_SIZE_CHF, to pre-fill the
calculator. No secret is read, and nothing but CSV-derived data is written.
"""

import csv
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCORED_CSV = Path("scored.csv")
SIGNALS_CSV = Path("signals.csv")
RAW_CSV = Path("raw_messages.csv")
OUT_HTML = Path(os.environ.get("REPORT_HTML", "report.html"))

DEFAULT_CHF_PER_POINT = 0.1
DEFAULT_POSITION_SIZE_CHF = 100.0
VERDICT_MIN_N = 50          # matches the pre-registered rule in README.md
STREAK_LEN = 10             # the streak window the calculator projects
PROJECTION_TRADES = 50
TEXT_TRUNCATE = 80

# Categorical series palette, fixed order, never cycled. Validated for CVD
# separation and lightness band against a white surface. Slot 8 (red) is
# deliberately unused: red means "negative expectancy" in this report and must
# not double as a channel identity.
SERIES_COLORS = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
]
MAX_CHARTED = len(SERIES_COLORS)
COLOR_CRITICAL = "#d03b3b"

csv.field_size_limit(10 * 1024 * 1024)


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def read_csv(path: Path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def num(value, default=None):
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_ts(value):
    """Tolerant ISO-8601 -> aware datetime, or None."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def fmt_gap(seconds: float) -> str:
    seconds = int(abs(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def truncate(text: str, limit: int = TEXT_TRUNCATE) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# --------------------------------------------------------------------------
# per-channel statistics
# --------------------------------------------------------------------------

def channel_stats(scored, signal_counts):
    """One dict per channel. Trades stay in timestamp order so the equity
    curve and the streak windows follow the sequence actually traded."""
    by_channel = {}
    for row in scored:
        net = num(row.get("points_net"))
        if net is None:
            continue
        by_channel.setdefault(row["channel"], []).append(
            {
                "ts": parse_ts(row.get("timestamp_utc")),
                "message_id": row.get("message_id", ""),
                "outcome": (row.get("outcome") or "").upper(),
                "net": net,
                "tp2_hit": (row.get("tp2_hit") or "").lower(),
                "tp3_hit": (row.get("tp3_hit") or "").lower(),
                "sl_distance": abs(
                    (num(row.get("entry_price")) or 0.0) - (num(row.get("sl")) or 0.0)
                ),
            }
        )

    stats = []
    for channel, trades in by_channel.items():
        trades.sort(key=lambda t: (t["ts"] or datetime.min.replace(tzinfo=timezone.utc)))
        nets = [t["net"] for t in trades]
        n = len(trades)
        wins = [t for t in trades if t["outcome"] == "WIN"]
        losses = [t for t in trades if t["outcome"] == "LOSS"]
        timeouts = [t for t in trades if t["outcome"] == "TIMEOUT"]
        win_nets = [t["net"] for t in wins] or [0.0]
        loss_nets = [t["net"] for t in trades if t["net"] < 0] or [0.0]

        # Running total, trade by trade: the equity curve.
        cumulative, running = [], 0.0
        for trade in trades:
            running += trade["net"]
            cumulative.append(running)

        # Observed worst/best window of consecutive trades, alongside the
        # theoretical streaks. The observed figure is what actually happened.
        windows = [
            sum(nets[i : i + STREAK_LEN]) for i in range(0, max(1, n - STREAK_LEN + 1))
        ] if n else [0.0]
        if n < STREAK_LEN:
            windows = [sum(nets)] if nets else [0.0]

        sl_distances = [t["sl_distance"] for t in trades if t["sl_distance"] > 0]
        expectancy = sum(nets) / n if n else 0.0
        trimmed = sorted(nets, reverse=True)[2:]

        stats.append(
            {
                "channel": channel,
                "total_signals": signal_counts.get(channel, n),
                "n": n,
                "wins": len(wins),
                "losses": len(losses),
                "timeouts": len(timeouts),
                "tp2_hits": sum(1 for t in trades if t["tp2_hit"] == "yes"),
                "tp2_defined": sum(1 for t in trades if t["tp2_hit"] in ("yes", "no")),
                "tp3_hits": sum(1 for t in trades if t["tp3_hit"] == "yes"),
                "tp3_defined": sum(1 for t in trades if t["tp3_hit"] in ("yes", "no")),
                "win_rate": 100.0 * len(wins) / n if n else 0.0,
                "expectancy": expectancy,
                "expectancy_trimmed": (sum(trimmed) / len(trimmed)) if trimmed else None,
                "avg_win": sum(win_nets) / len(win_nets),
                "avg_loss": sum(loss_nets) / len(loss_nets),
                "total_points": sum(nets),
                "worst_window": min(windows),
                "best_window": max(windows),
                "avg_sl_distance": (
                    sum(sl_distances) / len(sl_distances) if sl_distances else 0.0
                ),
                "trades": trades,
                "cumulative": cumulative,
            }
        )

    # Most-traded first: the channels with the strongest evidence lead, and the
    # colour a channel gets stays stable as long as its rank does.
    stats.sort(key=lambda s: (-s["n"], s["channel"]))
    for i, stat in enumerate(stats):
        stat["color"] = SERIES_COLORS[i] if i < MAX_CHARTED else None
    return stats


def deletion_events(raw):
    """One row per tampering event, newest first. 'Original text' is always the
    first captured version -- for an edit that is the text before the change."""
    first_text, first_ts = {}, {}
    for row in raw:
        key = (row.get("channel", ""), row.get("message_id", ""))
        if key not in first_text:
            first_text[key] = row.get("text", "")
            first_ts[key] = row.get("timestamp_utc", "")

    events, seen = [], set()
    for row in raw:
        key = (row.get("channel", ""), row.get("message_id", ""))
        posted = parse_ts(first_ts.get(key)) or parse_ts(row.get("timestamp_utc"))
        for kind, column in (("deleted", "deleted_detected_at"), ("edited", "edited_at")):
            stamp = (row.get(column) or "").strip()
            if not stamp:
                continue
            marker = (key, kind, stamp)
            if marker in seen:
                continue
            seen.add(marker)
            at = parse_ts(stamp)
            gap = (at - posted).total_seconds() if (at and posted) else None
            events.append(
                {
                    "channel": key[0],
                    "message_id": key[1],
                    "posted": first_ts.get(key, ""),
                    "kind": kind,
                    "at": stamp,
                    "gap": fmt_gap(gap) if gap is not None else "—",
                    "gap_seconds": gap,
                    "text": truncate(first_text.get(key, "")),
                }
            )
    events.sort(key=lambda e: e["at"], reverse=True)
    return events


# --------------------------------------------------------------------------
# section 3: equity curve, hand-built SVG (no libraries, no CDN)
# --------------------------------------------------------------------------

CHART_W, CHART_H = 640, 360
PAD_L, PAD_R, PAD_T, PAD_B = 54, 120, 18, 42


def nice_ticks(low, high, count=5):
    if high <= low:
        high = low + 1.0
    raw = (high - low) / max(1, count - 1)
    magnitude = 10 ** int(f"{raw:e}".split("e")[1])
    for step in (1, 2, 2.5, 5, 10):
        if magnitude * step >= raw:
            step *= magnitude
            break
    else:
        step = magnitude * 10
    start = step * (low // step)
    ticks, value = [], start
    while value <= high + step * 0.5:
        ticks.append(round(value, 6))
        value += step
    return ticks


def build_chart(stats):
    """Returns (svg_markup, points_for_js). Geometry is computed here; the
    inline JS only does nearest-point lookup for the tap readout."""
    charted = [s for s in stats if s["color"] and s["cumulative"]]
    if not charted:
        return "", []

    xs = [t["ts"].timestamp() for s in charted for t in s["trades"] if t["ts"]]
    if not xs:
        return "", []
    x_min, x_max = min(xs), max(xs)
    if x_max <= x_min:
        x_max = x_min + 3600.0

    values = [v for s in charted for v in s["cumulative"]] + [0.0]
    y_lo, y_hi = min(values), max(values)
    span = (y_hi - y_lo) or 1.0
    y_lo, y_hi = y_lo - span * 0.08, y_hi + span * 0.08

    plot_w = CHART_W - PAD_L - PAD_R
    plot_h = CHART_H - PAD_T - PAD_B

    def sx(seconds):
        return PAD_L + (seconds - x_min) / (x_max - x_min) * plot_w

    def sy(value):
        return PAD_T + (y_hi - value) / (y_hi - y_lo) * plot_h

    parts = [
        f'<svg class="equity" viewBox="0 0 {CHART_W} {CHART_H}" '
        f'role="img" aria-label="Cumulative net points per channel, trade by trade" '
        f'xmlns="http://www.w3.org/2000/svg">'
    ]

    # Loss territory: everything below zero, washed faintly. The zero line
    # itself is the reference, so it gets the stronger rule.
    zero_y = sy(0.0)
    if zero_y < PAD_T + plot_h:
        parts.append(
            f'<rect x="{PAD_L}" y="{zero_y:.1f}" width="{plot_w}" '
            f'height="{PAD_T + plot_h - zero_y:.1f}" fill="{COLOR_CRITICAL}" '
            f'opacity="0.05"/>'
        )

    # Horizontal grid: solid hairlines, one shade off the surface.
    for tick in nice_ticks(y_lo, y_hi):
        if not (y_lo <= tick <= y_hi):
            continue
        ty = sy(tick)
        parts.append(
            f'<line x1="{PAD_L}" y1="{ty:.1f}" x2="{PAD_L + plot_w}" y2="{ty:.1f}" '
            f'stroke="#e1e0d9" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{PAD_L - 8}" y="{ty + 4:.1f}" text-anchor="end" '
            f'class="tick">{"0" if abs(tick) < 1e-9 else f"{tick:+.0f}"}</text>'
        )

    parts.append(
        f'<line x1="{PAD_L}" y1="{zero_y:.1f}" x2="{PAD_L + plot_w}" '
        f'y2="{zero_y:.1f}" stroke="#c3c2b7" stroke-width="1.5"/>'
    )

    # Four date ticks along the bottom.
    for i in range(4):
        frac = i / 3
        seconds = x_min + (x_max - x_min) * frac
        tx = sx(seconds)
        anchor = "start" if i == 0 else ("end" if i == 3 else "middle")
        label = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%d %b")
        parts.append(
            f'<text x="{tx:.1f}" y="{PAD_T + plot_h + 20}" text-anchor="{anchor}" '
            f'class="tick">{label}</text>'
        )
    parts.append(
        f'<line x1="{PAD_L}" y1="{PAD_T + plot_h}" x2="{PAD_L + plot_w}" '
        f'y2="{PAD_T + plot_h}" stroke="#c3c2b7" stroke-width="1"/>'
    )
    parts.append(
        f'<text x="{PAD_L - 8}" y="{PAD_T - 4}" text-anchor="end" class="tick">pts</text>'
    )

    js_points, end_labels = [], []

    for index, stat in enumerate(charted):
        coords = [
            (sx(t["ts"].timestamp()), sy(cum), cum, t)
            for t, cum in zip(stat["trades"], stat["cumulative"])
            if t["ts"]
        ]
        if not coords:
            continue
        negative = stat["expectancy"] < 0
        # A negative-expectancy channel ends in red: the closing stretch of the
        # line is drawn in the critical colour inside a red halo, so the curve
        # visibly terminates in a loss zone.
        tail_len = max(2, -(-len(coords) * 15 // 100)) if negative else 0
        split = max(0, len(coords) - tail_len) if negative else len(coords)

        head = coords[: split if split > 0 else 1]
        tail = coords[max(0, split - 1) :] if negative else []

        def polyline(points):
            return " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in points)

        if tail:
            parts.append(
                f'<polyline points="{polyline(tail)}" fill="none" '
                f'stroke="{COLOR_CRITICAL}" stroke-width="10" stroke-linecap="round" '
                f'opacity="0.16"/>'
            )
        if len(head) > 1:
            parts.append(
                f'<polyline points="{polyline(head)}" fill="none" '
                f'stroke="{stat["color"]}" stroke-width="2" stroke-linejoin="round" '
                f'stroke-linecap="round"/>'
            )
        if tail:
            parts.append(
                f'<polyline points="{polyline(tail)}" fill="none" '
                f'stroke="{COLOR_CRITICAL}" stroke-width="2.5" stroke-linejoin="round" '
                f'stroke-linecap="round"/>'
            )

        # Markers only while they stay legible. Past ~18 points a marker per
        # trade reads as a dashed line rather than a series of trades, so the
        # line carries the shape and the tap readout carries the values.
        if len(coords) <= 18:
            for order, (x, y, _, _) in enumerate(coords):
                # Markers inside the red tail wear the tail's colour; a series
                # dot sitting on top of the red segment would undo it.
                fill = COLOR_CRITICAL if (negative and order >= split) else stat["color"]
                parts.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" '
                    f'fill="{fill}" stroke="#ffffff" stroke-width="1.5"/>'
                )

        last_x, last_y, last_cum, _ = coords[-1]
        parts.append(
            f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="5" '
            f'fill="{COLOR_CRITICAL if negative else stat["color"]}" '
            f'stroke="#ffffff" stroke-width="2"/>'
        )
        end_labels.append(
            {
                "x": last_x,
                "y": last_y,
                "color": stat["color"],
                "negative": negative,
                "name": stat["channel"],
                "cum": last_cum,
            }
        )

        for order, (x, y, cum, trade) in enumerate(coords):
            js_points.append(
                {
                    "x": round(x, 1),
                    "y": round(y, 1),
                    "s": index,
                    "i": order + 1,
                    "cum": round(cum, 2),
                    "net": round(trade["net"], 2),
                    "out": trade["outcome"],
                    "t": trade["ts"].strftime("%Y-%m-%d %H:%M") if trade["ts"] else "",
                    "id": trade["message_id"],
                }
            )

    # Direct end labels, nudged apart so they never overlap. Label text wears
    # the ink token; the dot beside it carries the series identity.
    end_labels.sort(key=lambda label: label["y"])
    for i in range(1, len(end_labels)):
        # A negative channel prints a second line beneath its name, so it needs
        # more clearance than a plain one.
        gap = 29.0 if end_labels[i - 1]["negative"] else 16.0
        if end_labels[i]["y"] - end_labels[i - 1]["y"] < gap:
            end_labels[i]["y"] = end_labels[i - 1]["y"] + gap
    for label in end_labels:
        ty = min(max(label["y"], PAD_T + 6), CHART_H - PAD_B + 8)
        parts.append(
            f'<circle cx="{PAD_L + plot_w + 10:.1f}" cy="{ty - 4:.1f}" r="4" '
            f'fill="{label["color"]}"/>'
        )
        name = html.escape(truncate(label["name"], 12))
        parts.append(
            f'<text x="{PAD_L + plot_w + 19:.1f}" y="{ty:.1f}" class="endlabel">'
            f'{name}</text>'
        )
        if label["negative"]:
            parts.append(
                f'<text x="{PAD_L + plot_w + 19:.1f}" y="{ty + 12:.1f}" '
                f'class="endlabel neg">▼ negative</text>'
            )

    # One transparent overlay carries every tap: the hit area is the whole plot
    # rather than a pinpoint marker, which is what makes this usable on a tablet.
    parts.append(
        f'<rect id="hit" x="{PAD_L}" y="{PAD_T}" width="{plot_w}" height="{plot_h}" '
        f'fill="transparent" style="cursor:crosshair"/>'
    )
    parts.append(
        '<circle id="cursor" r="7" fill="none" stroke="#0b0b0b" '
        'stroke-width="2" opacity="0"/>'
    )
    parts.append("</svg>")
    return "".join(parts), js_points


# --------------------------------------------------------------------------
# styling: white ground, dark ink, no frameworks. Tap targets >= 44px, no
# hover-only affordance, legible at 375px.
# --------------------------------------------------------------------------

CSS = """
:root {
  --surface: #ffffff;
  --plane: #f9f9f7;
  --ink: #0b0b0b;
  --ink-2: #52514e;
  --muted: #898781;
  --rule: #e1e0d9;
  --axis: #c3c2b7;
  --crit: #d03b3b;
  --good: #006300;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--surface);
  color: var(--ink);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 980px; margin: 0 auto; padding: 24px 16px 72px; }
h1 { font-size: 25px; line-height: 1.2; margin: 0 0 6px; }
h2 {
  font-size: 19px; margin: 44px 0 4px; padding-top: 22px;
  border-top: 1px solid var(--rule);
}
h2 .num { color: var(--muted); font-weight: 600; margin-right: 8px; }
p { margin: 8px 0; }
.lede { color: var(--ink-2); margin: 0 0 4px; }
.note { color: var(--ink-2); font-size: 14px; }
.muted { color: var(--muted); }
small { font-size: 13px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }

.scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 14px 0 6px; }
table { border-collapse: collapse; width: 100%; font-size: 15px;
        font-variant-numeric: tabular-nums; }
th, td { padding: 12px 10px; text-align: right; border-bottom: 1px solid var(--rule);
         white-space: nowrap; vertical-align: top; }
th:first-child, td:first-child { text-align: left; }
thead th { font-size: 13px; color: var(--ink-2); font-weight: 600;
           border-bottom: 1.5px solid var(--axis); }
tbody tr:last-child td { border-bottom: none; }
td .sub { display: block; font-size: 12px; color: var(--muted); font-weight: 400; }
td .sub.neg { color: var(--crit); }
.chan { font-weight: 600; }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
       margin-right: 7px; vertical-align: -1px; }
.neg { color: var(--crit); }
.pos { color: var(--good); }
.badge {
  display: inline-block; margin-top: 4px; padding: 2px 7px; border-radius: 4px;
  background: var(--plane); border: 1px solid var(--rule);
  font-size: 11px; font-weight: 600; color: var(--ink-2); letter-spacing: 0.02em;
  white-space: nowrap;
}
.badge.warn { border-color: var(--crit); color: var(--crit); }

.controls { display: flex; flex-wrap: wrap; gap: 16px; margin: 16px 0 4px; }
.field { flex: 1 1 220px; min-width: 180px; }
label { display: block; font-size: 14px; color: var(--ink-2); margin-bottom: 6px; }
input[type="number"] {
  width: 100%; min-height: 44px; padding: 10px 12px;
  font: 16px/1.4 system-ui, -apple-system, sans-serif;
  color: var(--ink); background: var(--surface);
  border: 1px solid var(--axis); border-radius: 8px;
}
input[type="number"]:focus { outline: 2px solid #2a78d6; outline-offset: 1px; }

.chartbox { margin: 16px 0 0; }
svg.equity { display: block; width: 100%; min-width: 600px; height: auto; }
svg.equity .tick { font: 12px system-ui, sans-serif; fill: var(--muted);
                   font-variant-numeric: tabular-nums; }
svg.equity .endlabel { font: 12px system-ui, sans-serif; fill: var(--ink-2); }
svg.equity .endlabel.neg { fill: var(--crit); font-weight: 600; }
.readout {
  margin: 10px 0 0; padding: 12px 14px; min-height: 44px;
  background: var(--plane); border: 1px solid var(--rule); border-radius: 8px;
  font-size: 15px; font-variant-numeric: tabular-nums;
}
.legend { display: flex; flex-wrap: wrap; gap: 10px 20px; margin: 14px 0 0;
          padding: 0; list-style: none; }
.legend li { font-size: 14px; color: var(--ink-2); min-height: 24px; }
.legend .name { color: var(--ink); font-weight: 600; }

.banner {
  margin: 20px 0; padding: 18px; border-radius: 10px;
  background: var(--plane); border: 1px solid var(--rule);
}
.banner strong { display: block; font-size: 18px; margin-bottom: 6px; }
.rules { padding-left: 22px; margin: 10px 0; }
.rules li { margin: 6px 0; color: var(--ink-2); }
.evtext { white-space: normal; max-width: 340px; text-align: left;
          font-size: 14px; color: var(--ink-2); }
footer { margin-top: 48px; padding-top: 20px; border-top: 1px solid var(--rule);
         color: var(--muted); font-size: 13px; }
@media (max-width: 420px) {
  .wrap { padding: 18px 12px 56px; }
  h1 { font-size: 22px; }
  th, td { padding: 12px 8px; }
}
"""


# --------------------------------------------------------------------------
# section builders
# --------------------------------------------------------------------------

def esc(value) -> str:
    return html.escape(str(value), quote=True)


def pct_cell(count, total):
    if total <= 0:
        return "—"
    return f'{count} <span class="sub">{100.0 * count / total:.0f}%</span>'


def section_summary(stats):
    head = (
        "<tr><th>Channel</th><th>Total Signals</th><th>Trades Scored</th>"
        "<th>TP1 Hit</th><th>TP2 Hit<sup>*</sup></th><th>TP3 Hit<sup>*</sup></th>"
        "<th>SL Hit</th><th>Timeout</th><th>Win Rate</th>"
        "<th>Avg Points per Trade</th></tr>"
    )
    rows = []
    for stat in stats:
        n = stat["n"]
        dot = (
            f'<span class="dot" style="background:{stat["color"]}"></span>'
            if stat["color"]
            else ""
        )
        badge = (
            f'<span class="badge warn">VERDICT WITHHELD · n&lt;{VERDICT_MIN_N}</span>'
            if n < VERDICT_MIN_N
            else ""
        )
        trimmed = stat["expectancy_trimmed"]
        trimmed_text = (
            f"{trimmed:+.2f} without best 2" if trimmed is not None else "best-2 n/a"
        )
        klass = "neg" if stat["expectancy"] < 0 else "pos"
        tp2 = pct_cell(stat["tp2_hits"], n) if stat["tp2_defined"] else "—"
        tp3 = pct_cell(stat["tp3_hits"], n) if stat["tp3_defined"] else "—"
        rows.append(
            f'<tr><td><span class="chan">{dot}{esc(stat["channel"])}</span>'
            + (f"<br>{badge}" if badge else "")
            + "</td>"
            f'<td>{stat["total_signals"]}</td><td>{n}</td>'
            f'<td>{pct_cell(stat["wins"], n)}</td><td>{tp2}</td><td>{tp3}</td>'
            f'<td>{pct_cell(stat["losses"], n)}</td>'
            f'<td>{pct_cell(stat["timeouts"], n)}</td>'
            f'<td>{stat["win_rate"]:.1f}%</td>'
            f'<td class="{klass}">{stat["expectancy"]:+.2f}'
            f'<span class="sub">{trimmed_text}</span></td></tr>'
        )
    return (
        '<h2><span class="num">1</span>Per-channel summary</h2>'
        '<p class="lede">Hit counts show the raw number with its share of '
        "scored trades beneath. Win rate and average points are net of 3 points "
        f"of round-trip costs. A channel with fewer than {VERDICT_MIN_N} scored "
        "trades gets no verdict, however good the numbers look.</p>"
        f'<div class="scroll"><table><thead>{head}</thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        '<p class="note"><sup>*</sup> TP2/TP3 are counterfactual: they record '
        "whether price reached that level before the stop or the 48-hour "
        "deadline under a hold-through-TP1 rule. The scored trade still exits at "
        "TP1, so these columns never feed expectancy. A dash means the channel "
        "never published that level.</p>"
    )


def section_calculator(stats, position_size):
    head = (
        "<tr><th>Channel</th><th>Per Trade</th>"
        f"<th>Over {PROJECTION_TRADES}</th><th>Over {PROJECTION_TRADES} %</th>"
        f"<th>Worst {STREAK_LEN} in a Row</th>"
        f"<th>Best {STREAK_LEN} in a Row</th>"
        "<th>Implied CHF/pt</th></tr>"
    )
    rows = []
    for stat in stats:
        payload = {
            "exp": round(stat["expectancy"], 4),
            "avgWin": round(stat["avg_win"], 4),
            "avgLoss": round(stat["avg_loss"], 4),
            "worst": round(stat["worst_window"], 4),
            "best": round(stat["best_window"], 4),
            "sl": round(stat["avg_sl_distance"], 4),
            "n": stat["n"],
        }
        rows.append(
            f'<tr data-calc=\'{esc(json.dumps(payload))}\'>'
            f'<td><span class="chan">{esc(stat["channel"])}</span></td>'
            '<td class="c-per"></td><td class="c-total"></td><td class="c-pct"></td>'
            '<td class="c-worst"></td><td class="c-best"></td><td class="c-implied"></td>'
            "</tr>"
        )
    return (
        '<h2><span class="num">2</span>Position size calculator</h2>'
        '<p class="lede">Point values converted to CHF at the rate below. These '
        "are projections from a small sample, not forecasts — a channel under "
        f"{VERDICT_MIN_N} trades has no established expectancy to project.</p>"
        '<div class="controls">'
        '<div class="field"><label for="risk">I am risking X CHF on each trade</label>'
        f'<input id="risk" type="number" inputmode="decimal" min="0" step="10" '
        f'value="{position_size:g}"></div>'
        '<div class="field"><label for="rate">CHF per point</label>'
        f'<input id="rate" type="number" inputmode="decimal" min="0" step="0.01" '
        f'value="{DEFAULT_CHF_PER_POINT:g}"></div>'
        "</div>"
        f'<div class="scroll"><table><thead>{head}</thead>'
        f"<tbody id=\"calcbody\">{''.join(rows)}</tbody></table></div>"
        '<p class="note"><strong>Per trade</strong> = average net points × CHF '
        "per point, with the risk-based figure beneath it: the same expectancy "
        "expressed in your risk units (average net points ÷ average stop "
        "distance × your risk per trade). They agree only when the rate above "
        "matches the position size your risk budget actually buys. Percentage "
        f"is measured against the {PROJECTION_TRADES} × risk you would stake to "
        "get there. The streak figures are "
        f"{STREAK_LEN} consecutive trades at this channel's average win and "
        "average loss; underneath each is the worst and best run of "
        f"{STREAK_LEN} consecutive trades that actually happened — for a "
        "negative channel the best real run can still be a loss. "
        "<strong>Implied CHF/pt</strong> is your risk divided by the channel's "
        "average stop distance — the rate your stated risk actually implies. "
        "When it diverges from the rate above by more than 3×, the row says by "
        "how much, in red. Expect that with the defaults: 0.10 CHF per point "
        "against a stop of a dozen-odd points is risking about 1 CHF a trade, "
        "not 100. Set one of the two fields to match the other before reading "
        "the CHF columns as money.</p>"
    )


def section_chart(stats, svg, uncharted):
    if not svg:
        return (
            '<h2><span class="num">3</span>Equity curve</h2>'
            '<p class="lede">No settled trades to plot yet.</p>'
        )
    legend = []
    for stat in stats:
        if not stat["color"]:
            continue
        tag = (
            ' <span class="neg">▼ negative expectancy</span>'
            if stat["expectancy"] < 0
            else ""
        )
        legend.append(
            f'<li><span class="dot" style="background:{stat["color"]}"></span>'
            f'<span class="name">{esc(stat["channel"])}</span> · '
            f'{stat["n"]} trades · {stat["expectancy"]:+.2f} pts/trade · '
            f'{stat["total_points"]:+.0f} pts total{tag}</li>'
        )
    extra = (
        f'<p class="note">{uncharted} further channel(s) are in the tables but '
        f"not on the chart: the palette carries {MAX_CHARTED} distinguishable "
        "series and past that the lines stop being tellable apart.</p>"
        if uncharted
        else ""
    )
    return (
        '<h2><span class="num">3</span>Equity curve</h2>'
        '<p class="lede">Cumulative net points, trade by trade, in the order '
        "each channel posted them. A channel whose expectancy is negative ends "
        "in a red zone.</p>"
        f'<div class="chartbox scroll">{svg}</div>'
        '<div class="readout" id="readout">Tap or click any point on the curve '
        "to read that trade.</div>"
        f'<ul class="legend">{"".join(legend)}</ul>{extra}'
    )


def section_evidence(events, scored):
    outcome_by_key = {
        (row.get("channel"), row.get("message_id")): (row.get("outcome") or "").upper()
        for row in scored
    }
    deletions = [e for e in events if e["kind"] == "deleted"]
    edits = [e for e in events if e["kind"] == "edited"]
    scored_deletions = [
        outcome_by_key.get((e["channel"], e["message_id"]))
        for e in deletions
        if (e["channel"], e["message_id"]) in outcome_by_key
    ]
    losing_deletions = sum(1 for o in scored_deletions if o in ("LOSS", "TIMEOUT"))

    verdict = (
        f"{len(deletions)} deletion(s) and {len(edits)} edit(s) detected. "
        f"{len(scored_deletions)} of the deleted messages had been scored; "
        f"{losing_deletions} of those were not wins."
        if deletions or edits
        else "No deletions or edits detected yet."
    )

    if not events:
        return (
            '<h2><span class="num">4</span>Deletion and edit log</h2>'
            f'<p class="lede">{verdict}</p>'
        )

    head = (
        "<tr><th>Channel</th><th>Message ID</th><th>Original Timestamp</th>"
        "<th>Deletion/Edit Time</th><th>Time Gap</th><th>Original Text</th></tr>"
    )
    rows = []
    for event in events:
        kind_class = "neg" if event["kind"] == "deleted" else "muted"
        rows.append(
            f'<tr><td>{esc(event["channel"])}</td>'
            f'<td>{esc(event["message_id"])}</td>'
            f'<td>{esc(event["posted"])}</td>'
            f'<td>{esc(event["at"])}'
            f'<span class="sub {kind_class}">{event["kind"]}</span></td>'
            f'<td>{esc(event["gap"])}</td>'
            f'<td class="evtext">{esc(event["text"])}</td></tr>'
        )
    return (
        '<h2><span class="num">4</span>Deletion and edit log</h2>'
        f'<p class="lede">{verdict} Every row here is a message the channel '
        "changed or removed after posting. Original text is always the first "
        "version captured.</p>"
        f'<div class="scroll"><table><thead>{head}</thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
        '<p class="note">This log can only show what was captured. A signal '
        "posted and deleted between two 20-minute polls leaves no trace at all, "
        "so these counts are a lower bound.</p>"
    )


# --------------------------------------------------------------------------
# inline behaviour: the calculator and the chart's tap readout
# --------------------------------------------------------------------------

JS = """
(function () {
  var DATA = window.__AUDIT__ || { points: [], series: [] };

  function chf(value) {
    return (value < 0 ? "-" : "+") + Math.abs(value).toFixed(2) + " CHF";
  }
  function cls(value) { return value < 0 ? "neg" : "pos"; }

  // ---- section 2: live position sizing -----------------------------------
  var riskInput = document.getElementById("risk");
  var rateInput = document.getElementById("rate");

  function recalc() {
    if (!riskInput || !rateInput) { return; }
    var risk = parseFloat(riskInput.value);
    var rate = parseFloat(rateInput.value);
    if (!isFinite(risk) || risk < 0) { risk = 0; }
    if (!isFinite(rate) || rate < 0) { rate = 0; }

    var rows = document.querySelectorAll("#calcbody tr[data-calc]");
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var d;
      try { d = JSON.parse(row.getAttribute("data-calc")); } catch (e) { continue; }

      var per = d.exp * rate;
      var total = per * """ + str(PROJECTION_TRADES) + """;
      var staked = risk * """ + str(PROJECTION_TRADES) + """;
      var worstTheory = d.avgLoss * """ + str(STREAK_LEN) + """ * rate;
      var bestTheory = d.avgWin * """ + str(STREAK_LEN) + """ * rate;

      // Risk-based view: what the same expectancy is worth if the position is
      // sized off the risk budget instead of the fixed rate.
      var riskBased = d.sl > 0 ? (d.exp / d.sl) * risk : 0;
      set(
        row, ".c-per", chf(per), per,
        d.sl > 0 ? "risk-based " + chf(riskBased) : ""
      );
      set(row, ".c-total", chf(total), total);
      set(
        row, ".c-pct",
        staked > 0 ? (total / staked * 100).toFixed(1) + "%" : "\\u2014",
        total
      );
      set(
        row, ".c-worst", chf(worstTheory), worstTheory,
        "observed " + chf(d.worst * rate)
      );
      set(
        row, ".c-best", chf(bestTheory), bestTheory,
        "observed " + chf(d.best * rate)
      );
      var implied = d.sl > 0 && risk > 0 ? risk / d.sl : 0;
      var mismatch =
        implied > 0 && rate > 0 && (implied / rate > 3 || rate / implied > 3);
      var factor = "";
      if (mismatch) {
        factor = implied > rate
          ? " \\u00b7 " + Math.round(implied / rate) + "\\u00d7 the rate above"
          : " \\u00b7 1/" + Math.round(rate / implied) + " of the rate above";
      }
      set(
        row, ".c-implied",
        implied > 0 ? implied.toFixed(3) : "\\u2014", 0,
        d.sl > 0 ? "stop " + d.sl.toFixed(1) + " pts" + factor : "",
        mismatch ? "neg" : ""
      );
    }
  }

  function set(row, selector, text, value, sub, subClass) {
    var cell = row.querySelector(selector);
    if (!cell) { return; }
    cell.textContent = text;
    cell.className = selector.slice(1) + (value ? " " + cls(value) : "");
    if (sub) {
      var span = document.createElement("span");
      span.className = "sub" + (subClass ? " " + subClass : "");
      span.textContent = sub;
      cell.appendChild(span);
    }
  }

  if (riskInput && rateInput) {
    riskInput.addEventListener("input", recalc);
    rateInput.addEventListener("input", recalc);
    recalc();
  }

  // ---- section 3: tap-to-read on the equity curve -------------------------
  // Click, not hover: on a tablet there is no hover, and every value here is
  // also in the tables, so the readout enhances rather than gates.
  var svg = document.querySelector("svg.equity");
  var readout = document.getElementById("readout");
  var cursor = document.getElementById("cursor");

  if (svg && readout && DATA.points.length) {
    svg.addEventListener("click", function (event) {
      var pt = toLocal(event);
      if (!pt) { return; }
      var best = null, bestDistance = Infinity;
      for (var i = 0; i < DATA.points.length; i++) {
        var p = DATA.points[i];
        var dx = p.x - pt.x, dy = p.y - pt.y;
        var distance = dx * dx + dy * dy;
        if (distance < bestDistance) { bestDistance = distance; best = p; }
      }
      if (!best) { return; }
      var name = DATA.series[best.s] || "";
      readout.textContent =
        name + " \\u00b7 trade " + best.i + " of " + DATA.counts[best.s] +
        " \\u00b7 " + best.t + " UTC \\u00b7 message #" + best.id +
        " \\u00b7 " + best.out + " " +
        (best.net >= 0 ? "+" : "-") + Math.abs(best.net).toFixed(2) +
        " pts \\u00b7 running total " +
        (best.cum >= 0 ? "+" : "-") + Math.abs(best.cum).toFixed(2) + " pts";
      if (cursor) {
        cursor.setAttribute("cx", best.x);
        cursor.setAttribute("cy", best.y);
        cursor.setAttribute("opacity", "1");
      }
    });
  }

  function toLocal(event) {
    if (!svg.getScreenCTM) { return null; }
    var ctm = svg.getScreenCTM();
    if (!ctm) { return null; }
    var point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    return point.matrixTransform(ctm.inverse());
  }
})();
"""


PLACEHOLDER_TEXT = "No scored signals yet — check back after the first scoring run"


def render(stats, svg, js_points, events, scored, position_size, counts, uncharted):
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if stats:
        body = (
            section_summary(stats)
            + section_calculator(stats, position_size)
            + section_chart(stats, svg, uncharted)
            + section_evidence(events, scored)
        )
    else:
        body = (
            f'<div class="banner"><strong>{esc(PLACEHOLDER_TEXT)}</strong>'
            "<span class=\"note\">The summary table, the position size "
            "calculator and the equity curve appear here once score.py has "
            "settled its first trade. Capture keeps running in the meantime, so "
            "the evidence log below may already have something in it.</span></div>"
            + section_evidence(events, scored)
        )

    payload = {
        "points": js_points,
        "series": [stat["channel"] for stat in stats if stat["color"]],
        "counts": [len(stat["cumulative"]) for stat in stats if stat["color"]],
    }
    embedded = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Telegram Signal Audit — Report</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<h1>Telegram Signal Audit</h1>
<p class="lede">Generated {generated} · {counts["channels"]} channel(s) ·
{counts["signals"]} parsed signal(s) · {counts["scored"]} settled trade(s) ·
{counts["deletions"]} deletion(s) · {counts["edits"]} edit(s)</p>
<p class="note">Net of 3 points of round-trip costs. TP1 and SL inside the same
5-minute candle is scored as a loss. Maximum holding period 48 hours.</p>
{body}
<footer>
<p><strong>The rules this report obeys.</strong></p>
<ul class="rules">
<li>No verdict on a channel until it has {VERDICT_MIN_N} scored trades, however
good the numbers look before then.</li>
<li>Expectancy is always shown twice — as measured, and with the best two trades
removed. If the second number collapses, the edge was two trades.</li>
<li>Pre-registered stopping rule: if every channel tested comes out negative, the
investigation closes. No adding one more group.</li>
</ul>
<p>Deletion counts are a lower bound: a signal posted and deleted inside one
20-minute polling window is never captured. Full methodology and limitations are
in the repository README.</p>
</footer>
</div>
<script>window.__AUDIT__ = {embedded};</script>
<script>{JS}</script>
</body>
</html>
"""


def main():
    scored = read_csv(SCORED_CSV)
    signals = read_csv(SIGNALS_CSV)
    raw = read_csv(RAW_CSV)

    position_size = num(
        os.environ.get("POSITION_SIZE_CHF"), DEFAULT_POSITION_SIZE_CHF
    )
    if position_size is None or position_size < 0:
        position_size = DEFAULT_POSITION_SIZE_CHF

    signal_counts = {}
    for row in signals:
        signal_counts[row["channel"]] = signal_counts.get(row["channel"], 0) + 1

    stats = channel_stats(scored, signal_counts)
    events = deletion_events(raw)
    svg, js_points = build_chart(stats)
    uncharted = sum(1 for stat in stats if not stat["color"])

    counts = {
        "channels": len({row["channel"] for row in raw} | set(signal_counts)) or len(stats),
        "signals": len(signals),
        "scored": len(scored),
        "deletions": sum(1 for e in events if e["kind"] == "deleted"),
        "edits": sum(1 for e in events if e["kind"] == "edited"),
    }

    OUT_HTML.write_text(
        render(
            stats, svg, js_points, events, scored, position_size, counts, uncharted
        ),
        encoding="utf-8",
    )

    size_kb = OUT_HTML.stat().st_size / 1024
    if not stats:
        print(f"[info] {PLACEHOLDER_TEXT}; wrote placeholder {OUT_HTML}")
    else:
        print(f"[info] wrote {OUT_HTML} ({size_kb:.1f} KB)")
        for stat in stats:
            flag = " (VERDICT WITHHELD)" if stat["n"] < VERDICT_MIN_N else ""
            print(
                f"[info]   {stat['channel']}: n={stat['n']} "
                f"exp={stat['expectancy']:+.2f} pts{flag}"
            )
    print(
        f"[info] {counts['deletions']} deletion(s), {counts['edits']} edit(s) "
        f"in the evidence log"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
