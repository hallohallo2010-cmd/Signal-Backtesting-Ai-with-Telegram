#!/usr/bin/env python3
"""Basis estimation, contract rolls, and the two scoring modes.

Separated from score.py so the awkward parts can be tested on their own.

Why any of this exists: the channels quote SPOT gold; the only intraday series
available is GC=F, the FUTURE, which trades at a carry premium. Comparing a
spot level to a futures price shifts every level by that premium -- measured
around 40 points here -- which put 95% of trades on the wrong side of their own
stop or target before they began.
"""

from datetime import timedelta
from statistics import median

# Signals within this many days of each other inform one basis estimate. Wide
# enough that a thin day is not dominated by its own one trade, short enough to
# track a premium that decays toward expiry.
BASIS_WINDOW_DAYS = 5

# A pending order that never trades at its level is not a trade. It expires at
# whichever comes first: this long, or the channel's next signal -- by which
# point the old level is superseded in practice.
PENDING_EXPIRY_HOURS = 24


def contract_roll_dates(start, end):
    """Approximate front-month roll boundaries for COMEX gold, from the
    contract calendar rather than from the price series.

    Gap detection was tried first and does not work here: every large gap in
    this series is the Sunday reopen, and thresholding cannot separate a weekly
    gap from a contract change. It flagged six "rolls", all at 22:10 on a
    Sunday, and none of them real.

    The deliverable months are Feb, Apr, Jun, Aug, Oct and Dec, and the front
    month rolls late in the month before delivery. The 25th is taken as the
    boundary: approximate to a few days, deterministic, and -- unlike the gap
    heuristic -- never wrong about a weekend.
    """
    roll_months = (1, 3, 5, 7, 9, 11)
    out = []
    year = start.year
    while year <= end.year:
        for month in roll_months:
            boundary = start.replace(
                year=year, month=month, day=25,
                hour=0, minute=0, second=0, microsecond=0,
            )
            if start <= boundary <= end:
                out.append(boundary)
        year += 1
    return sorted(out)


def basis_at(observations, when, roll_boundaries=(), window_days=BASIS_WINDOW_DAYS):
    """Estimated futures-minus-spot premium at a point in time.

    observations: (timestamp, fill_price - stated_entry) pairs, every signal
    that has a stated entry. The median of the neighbouring window, never a
    single day: a day with one signal would otherwise define the basis as that
    signal's own residual and force it to zero, which is circular.

    The window is clipped so it never straddles a contract roll, because the
    premium steps at a roll and a median across the step describes neither side.
    """
    if not observations:
        return None

    low = when - timedelta(days=window_days)
    high = when + timedelta(days=window_days)
    # Clip to the contract segment containing `when`.
    for boundary in roll_boundaries:
        if boundary <= when and boundary > low:
            low = boundary
        if boundary > when and boundary < high:
            high = boundary

    inside = [d for ts, d in observations if low <= ts <= high]
    if not inside:
        return None
    return median(inside)


def adjust_levels(signal, basis):
    """The signal's spot levels restated in futures terms.

    Adding the premium moves a spot level onto the series the walk runs on.
    This is the headline mode: the follower enters at the market price they can
    actually get, and the levels are where the channel put them.
    """
    return {
        "entry": None if signal.get("entry_stated") in (None, "") else float(signal["entry_stated"]) + basis,
        "tp1": signal["tp1"] + basis,
        "tp2": None if signal.get("tp2") is None else signal["tp2"] + basis,
        "tp3": None if signal.get("tp3") is None else signal["tp3"] + basis,
        "sl": signal["sl"] + basis,
    }


def distance_levels(signal, fill):
    """The signal's levels as distances, re-anchored on the actual fill.

    This is the isolation mode: it asks whether the setup was good, with the
    delay between posting and fill handed back to the channel. A follower who
    enters late does not get this -- their target is nearer and their stop
    further -- which is exactly why it is reported beside the headline rather
    than instead of it.
    """
    stated_entry = signal.get("entry_stated")
    if stated_entry in (None, ""):
        return None
    stated_entry = float(stated_entry)
    return {
        "entry": fill,
        "tp1": fill + (signal["tp1"] - stated_entry),
        "tp2": None if signal.get("tp2") is None else fill + (signal["tp2"] - stated_entry),
        "tp3": None if signal.get("tp3") is None else fill + (signal["tp3"] - stated_entry),
        "sl": fill + (signal["sl"] - stated_entry),
    }


def entry_already_past(levels, fill, is_long):
    """True when the fill sits beyond the trade's own target or stop.

    Not a trade a follower could take: the move was over, or the stop was
    already breached, before the signal was actionable. Scored as MISSED and
    kept out of expectancy rather than booked as a win or a loss.
    """
    if is_long:
        return fill >= levels["tp1"] or fill <= levels["sl"]
    return fill <= levels["tp1"] or fill >= levels["sl"]
