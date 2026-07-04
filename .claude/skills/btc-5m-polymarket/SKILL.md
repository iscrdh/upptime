---
name: btc-5m-polymarket
description: Run, monitor and report BTC 5-minute Up/Down momentum trading on Polymarket (entry near close, BTC impulse confirmation, skew threshold, stop-loss, optional micro-hedge). Use when the user asks to trade or watch Polymarket BTC 5m markets, start/stop the bot, run a dry-run, or get a PnL report.
---

# BTC 5m Polymarket (Claude Code skill)

Self-contained momentum runner for Polymarket's `btc-updown-5m-*` markets.
Everything lives inside this skill directory — no external trading repo is
required. Orders are placed directly on the Polymarket CLOB via
`py-clob-client`.

## Paths (all relative to this skill directory)
- Runner (one trade per invocation): `scripts/btc5m_trade.py`
- Control wrapper (start/status/stop/report/logs): `scripts/btc5m_ctl.sh`
- PnL/report utility: `scripts/btc5m_report.py`
- Profiles and guards: `config/btc_5m_profiles.yaml`
- Runtime logs and daily risk state: `runtime/` (git-ignored)
- Credentials: `.env` at the skill root (git-ignored), or `BTC5M_ENV_FILE`

## Safety rules for Claude (MANDATORY)
1. **Dry-run is the default everywhere.** Never pass `--execute` unless the
   user explicitly asked for LIVE trading in the current conversation. "Start
   the bot" is ambiguous — ask once, or start in dry-run and say so.
2. Never print, echo, or commit the contents of `.env` (it holds the wallet
   private key and API credentials).
3. Do not raise stakes, disable guards (`--no-impulse-check`), or loosen the
   config beyond what the user explicitly requested.
4. Use `scripts/btc5m_ctl.sh stop` to stop a live session — it lets the
   runner close any open position gracefully. Never `kill -9` first.
5. After every session, report the final JSON (result, side, entry price,
   close reason, PnL) back to the user.

## Setup (first run)
```bash
cd .claude/skills/btc-5m-polymarket
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
For live trading, create `.env` at the skill root:
```
PM_PRIVATE_KEY=0x...          # Polygon wallet private key
PM_FUNDER=0x...               # proxy/funder address shown in Polymarket
PM_SIGNATURE_TYPE=2           # 2=browser wallet proxy, 1=email, 0=EOA
# Optional; derived automatically from the key when absent:
# PM_API_KEY=... PM_API_SECRET=... PM_API_PASSPHRASE=...
```

## Run
Dry-run (safe, simulates fills and PnL at real market prices):
```bash
scripts/btc5m_ctl.sh start --profile conservative
```
Live (only on explicit user request):
```bash
scripts/btc5m_ctl.sh start --profile conservative --execute
```
Operate:
```bash
scripts/btc5m_ctl.sh status
scripts/btc5m_ctl.sh logs
scripts/btc5m_ctl.sh report --limit 20
scripts/btc5m_ctl.sh stop      # graceful: closes open position first
```
One-shot in foreground (useful for testing parameter changes):
```bash
.venv/bin/python scripts/btc5m_trade.py --profile conservative --entry-timeout-min 10
```
Common overrides (forwarded by `start`): `--stake-usd N`, `--threshold N`,
`--entry-timeout-min N`, `--btc-move-usd N`, `--poll-sec N`.

## Strategy (momentum into close)
1. Only trade the **current** 5m slot; entry window ~150–60 seconds before
   close (target ≈2 minutes left).
2. A side qualifies when its CLOB best ask ≥ `threshold_price` (0.70
   conservative) — the crowd already leans that way.
3. **BTC impulse confirmation**: the BTC spot move since the interval opened
   (Binance, Coinbase fallback) must agree in direction and be ≥
   `btc_move_usd_min` ($70). Enter WITH momentum, never against it.
4. Guards before entry: spread ≤ `max_spread`, top-of-book ask notional ≥
   `min_top_ask_notional_usd`, quote age ≤ 8s, daily loss/trade caps not hit.
5. After entry: stop-loss at `stop_loss_pct_from_entry` below entry (on the
   executable best bid), forced exit `exit_before_sec` (20s) before close.
6. Optional micro-hedge: on extreme skew (held side bid ≥ 0.95 with ≤45s
   left) buy $1–2 of the opposite side to cap last-second reversal risk.
7. Kill switch: 5 consecutive API errors abort the session (closing any open
   position first).

## Runtime behavior
- One invocation = one trade, then the process exits with a final indented
  JSON report (the compact JSON lines before it are heartbeats/events).
- `runtime/daily_state.json` tracks live trades and realized PnL per UTC day
  and blocks new sessions once `max_trades_per_day` or `daily_max_loss_usd`
  is hit (live mode only).
- To trade continuously, restart via `btc5m_ctl.sh start` after each run
  finishes (check `status` first). Do not run two sessions concurrently —
  the pidfile prevents it.
- SIGTERM/SIGINT during an open position triggers a real close before exit.

## Troubleshooting
- `no_entry_timeout`: normal — no qualifying signal inside the window.
- `blocked_by_daily_caps`: daily risk limits reached; do not override without
  explicit user instruction.
- `skip_impulse_feed_unavailable`: BTC price feeds unreachable; the runner
  never enters blind. Repeated occurrences trip the kill switch.
- Close retries/fallbacks (`fak` → `gtc` → `force_gtc`) are recorded in
  `close_debug` inside the final report.
