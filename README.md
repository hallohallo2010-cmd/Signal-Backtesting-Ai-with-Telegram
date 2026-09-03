# Telegram Signal Audit

An append-only capture and scoring rig for measuring what paid Telegram signal
groups actually deliver, as opposed to what their channel history says they
delivered after they have finished editing it.

The whole design rests on one assumption: **the channel is an unreliable
narrator.** Losing calls get deleted, entries get edited after the fact, and
screenshots get cropped. So the audit keeps its own copy, timestamps every
observation, and treats a deletion as a finding rather than as a correction.

Runs entirely on the GitHub Actions free tier. No laptop, no server.

---

## The rules

These are pre-registered. They are written down here so they cannot be
renegotiated once the numbers come in.

1. **No verdict on any channel until n >= 50 scored trades.** Below that,
   report the sample and say the verdict is withheld. `score.py` prints
   `VERDICT WITHHELD` for every channel under the threshold; do not narrate
   around it.
2. **Always report expectancy twice: raw, and with that channel's best 2 trades
   removed.** Two lucky calls can carry a 60-trade sample. If the trimmed
   figure collapses, the edge was those two trades, not a method.
3. **Pre-registered stopping rule: if every channel tested shows negative
   expectancy, the investigation closes.** No adding "one more group." Testing
   groups until one of them looks good is how you manufacture a false positive
   out of noise, and it is the exact failure mode this repo exists to avoid.
4. **A deleted message stays in the sample.** Deleted calls are scored like any
   other. Dropping them would reproduce the channel's own survivorship bias,
   which is the thing being measured.
5. **Scoring uses the first captured version of a message.** Later edits are
   preserved as separate rows and are evidence of tampering, but they never
   replace the call as originally posted.

---

## Layout

| File | Role |
| --- | --- |
| `.github/workflows/capture.yml` | Cron every 20 minutes + manual dispatch |
| `capture.py` | Telegram -> `raw_messages.csv` (append-only) |
| `parse.py` | `raw_messages.csv` -> `signals.csv` |
| `score.py` | `signals.csv` + price data -> `scored.csv` + report |
| `channels.txt` | One `@username` per line, `#` for comments |

Data files, created on first run:

- `raw_messages.csv` — the evidence log. Never edit this by hand.
- `signals.csv` — derived, regenerated from scratch on every parse.
- `scored.csv` — append-only; a signal already scored is never re-scored.

---

## Setup

### 1. Telegram credentials

Get `api_id` / `api_hash` from <https://my.telegram.org> → API development
tools. Then generate a `StringSession` **on your own machine** — this is an
interactive login and cannot be done in CI:

```bash
pip install telethon
python - <<'PY'
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
api_id = int(input("api_id: "))
api_hash = input("api_hash: ")
with TelegramClient(StringSession(), api_id, api_hash) as client:
    print(client.session.save())
PY
```

The string it prints is a **full login to your Telegram account**. Treat it
like a password:

- Paste it straight into the repo secret. Do not save it to a file, do not
  paste it into a chat, do not commit it.
- Anyone who reads it can read your Telegram as you. Revoke it from Telegram →
  Settings → Devices if it is ever exposed.
- Use a burner account, not your main one. Userbot automation is against
  Telegram's ToS and accounts do get limited or banned for it. Losing a burner
  is an inconvenience; losing your main number is not.

### 2. Repository secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
| --- | --- |
| `TG_API_ID` | numeric api_id |
| `TG_API_HASH` | api_hash |
| `TG_SESSION` | the StringSession string |

Nothing in this repo ever prints, logs, echoes, or commits these values.
`capture.py` reports missing secrets by **name only**. Keep it that way: a
`print(session)` added for debugging leaks a full account login into a public
Actions log.

### 3. Channels

Add one `@username` per line to `channels.txt`. The account behind `TG_SESSION`
must already be a member of each one — `capture.py` does not join anything.

Add a channel the day you start paying. Backfill is limited to the last 100
messages, and anything deleted before your first capture is gone for good.

### 4. Running

The workflow runs itself every 20 minutes and commits changed CSVs back to the
branch. To analyse:

```bash
pip install telethon pandas yfinance
python parse.py    # rebuilds signals.csv, prints unparsed counts per channel
python score.py    # appends to scored.csv, prints the report
```

Or run the workflow manually with **Run analysis** ticked, which does the same
thing in CI.

---

## Methodology

### Capture

`capture.py` fetches the last 100 messages per channel and appends any
`message_id` it has not seen before to `raw_messages.csv`:

```
channel, message_id, timestamp_utc, text, captured_at_utc, edited_at, deleted_detected_at
```

**Append-only.** Rows are never rewritten or removed. The single permitted
mutation of an existing row is stamping `deleted_detected_at`, because a
deletion is itself the finding and there is nowhere else to record it.

**Deletion detection.** A stored `message_id` that falls *inside the ID range
actually returned by the API* but is no longer served gets the current UTC time
stamped into `deleted_detected_at`. The range check matters: a message older
than the oldest fetched ID has merely fallen out of the 100-message window, and
its absence proves nothing. An empty fetch — network trouble, lost access — is
never read as a mass deletion.

**Edit detection.** If a message's text differs from what is stored, a **new
row** is appended carrying the new text and `edited_at`. The original row is
left exactly as captured. A group that edits a signal after the fact produces
two rows, and the diff between them is the record.

### Parsing

`parse.py` keeps every regex in a `PATTERNS` dict at the top of the file, keyed
by channel, with a `__default__` fallback. Tune a group's format there; the
parsing logic never needs to change. Copy the commented example block, key it
to the channel's `@username`, and override only the fields that differ.

A message becomes a signal only if it yields a direction, a symbol, TP1 and SL —
the minimum needed to score it. Level sets that contradict their own direction
(a long whose stop sits above its target) are rejected as mis-parses. Prices
outside a sanity band are treated as absent, so a date or a pip count cannot be
mistaken for a level.

`parse_confidence` is the weighted fraction of fields recovered: direction 0.25,
symbol 0.25, TP1 0.2, SL 0.2, entry 0.1. Extra take-profits do not add
confidence.

Anything that does not match stays in the raw log only, and the per-channel
unparsed count is printed on every run. **A high unparsed count means that
channel's patterns need work — not that the group posts few signals.** Read it
that way, or you will quietly under-sample the groups whose formatting you
never got around to tuning.

### Scoring

Price data is `GC=F` (COMEX gold futures) 5-minute candles from yfinance. Only
gold signals are priceable; anything else is left unscored and reported
separately.

For each unscored signal, `score.py` walks the price path forward from the
signal's timestamp and asks which came first, TP1 or SL.

- **Fill:** the open of the first candle at or after the signal's timestamp —
  what a subscriber acting on the alert would plausibly get. The group's stated
  entry is recorded as `entry_stated` but is not used as a fill; assuming a
  limit order filled is exactly the kind of favour this audit does not do.
- **Same-candle rule:** if TP1 and SL both fall inside one 5-minute candle,
  the trade is a **LOSS**. Intra-candle ordering is unknowable, so it takes the
  conservative call every time. These trades are flagged
  `same_candle_conflict=yes` and counted at the bottom of the report.
- **Costs:** 3 points per round trip, charged to every closed trade, including
  timeouts.
- **Max hold:** 48 hours. Anything unresolved closes flat at the last price and
  is still charged costs.
- **Not yet resolvable:** if the price data does not reach 48 hours past the
  signal, it is left unscored and picked up on a later run. It is never closed
  early.

Then, per channel: n, win rate, expectancy in points, and expectancy recomputed
with the best 2 trades removed.

One consequence worth knowing: a trade can be marked `WIN` with negative net
points. That happens when price gapped past TP1 before the fill — the outcome
is "TP1 hit first", but exiting at the group's TP level was worse than the fill.
Marking it at TP rather than at the better open is deliberate and conservative.

---

## Known limitations

Every one of these makes the measured record *better* than reality, except
where noted. None of them are fixed by trying harder in this repo.

**20-minute polling misses same-window deletions.** A signal posted and deleted
inside one 20-minute window is never captured at all — it leaves no trace in
`raw_messages.csv`, and the audit cannot know it existed. This biases results
*in the channel's favour*, since the calls most likely to be deleted fast are
the ones that went wrong fast.

Fixing this properly needs a live Telethon listener holding an open connection
and handling `MessageDeleted` events in real time. That requires an always-on
host, which is out of scope here by design — this rig is built to run on the
GitHub Actions free tier and nothing else. **Treat every deletion count in this
repo as a lower bound.**

**GitHub's scheduler is best effort.** Cron jobs run late under load and are
occasionally skipped entirely; scheduled workflows are also disabled
automatically after 60 days of repository inactivity. Real gaps between
captures will sometimes exceed 20 minutes, which widens the window above.
`captured_at_utc` records what actually happened — check it before trusting an
inter-capture interval.

**Intraday price history is ~60 days.** yfinance serves roughly 60 days of
5-minute data. A signal that ages past that can never be scored, and losing
old signals is not neutral — it silently reshapes the sample. `score.py` prints
a loud warning as soon as any unscored signal passes 50 days. Do not ignore it.

**Fills are idealised.** Exits are marked at the group's exact TP and SL levels
with no slippage beyond the flat 3-point round trip. Stops in fast markets fill
worse than that, so real stop losses are understated.

**Only the last 100 messages per channel are fetched.** A channel posting more
than 100 messages between captures will drop the overflow, and those messages
also fall outside the deletion-detection range afterwards.

**Parser coverage is not uniform.** Until a channel has a tuned `PATTERNS`
entry, its unparsed rate is higher, so its n is lower. Compare unparsed counts
across channels before comparing their track records.

**Symbol coverage is gold only.** The price layer prices `GC=F`. Non-gold calls
are captured and parsed but not scored.

**This measures signals, not subscriber outcomes.** Position sizing, execution
delay, spread, and swap are all outside the model. A positive expectancy here
is a necessary condition for a group being worth paying for, never a sufficient
one.
