---
name: btc-5m-polymarket
description: Run, monitor and report BTC 5-minute Up/Down trading on Polymarket. Value mode (default) prices the market with a volatility model and only enters on mispricing, holding to resolution; legacy momentum mode buys the leading side near close. Use when the user asks to trade or watch Polymarket BTC 5m markets, start/stop the bot, run a dry-run, collect calibration data, or get a PnL report.
---

# BTC 5m Polymarket (Claude Code skill)

Self-contained runner for Polymarket's `btc-updown-5m-*` markets. Orders are
placed directly on the Polymarket CLOB via `py-clob-client` — no external
trading repo needed.

## Paths (all relative to this skill directory)
- Runner (one trade per invocation): `scripts/btc5m_trade.py`
- Control wrapper (start/status/stop/report/logs): `scripts/btc5m_ctl.sh`
- Data collector (no orders, calibration dataset): `scripts/btc5m_collect.py`
- Edge evaluator (calibration + simulated PnL): `scripts/btc5m_eval.py`
- PnL/report utility: `scripts/btc5m_report.py`
- Profiles and guards: `config/btc_5m_profiles.yaml`
- Runtime logs, dataset and daily risk state: `runtime/` (git-ignored)
- Credentials: `.env` at the skill root (git-ignored), or `BTC5M_ENV_FILE`

## Safety rules for Claude (MANDATORY)
1. **Dry-run is the default everywhere.** Never pass `--execute` unless the
   user explicitly asked for LIVE trading in the current conversation. "Start
   the bot" is ambiguous — ask once, or start in dry-run and say so.
2. Never print, echo, or commit the contents of `.env` (it holds the wallet
   private key and API credentials).
3. Do not raise stakes, lower `edge_min`, or loosen guards beyond what the
   user explicitly requested.
4. Use `scripts/btc5m_ctl.sh stop` to stop a live session — it lets the
   runner settle any open position gracefully. Never `kill -9` first.
5. After every session, report the final JSON (result, side, entry price,
   model edge, close reason, PnL) back to the user.
6. If the user asks whether the strategy is profitable, do not speculate —
   point them to `btc5m_collect.py` + `btc5m_eval.py` and interpret the
   numbers honestly (see "Measuring edge" below).

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

## Strategy modes

### value (default) — trade mispricing, not momentum
1. Model the true probability that BTC finishes the interval up:
   `P(up) = Φ(move_so_far / (σ₁ₘ · √(seconds_left/60)))` with σ₁ₘ estimated
   from the last hour of 1-minute candles (Binance, Coinbase fallback).
2. Enter a side only when `model_prob − ask − fee_buffer ≥ edge_min`
   (0.05 conservative / 0.03 aggressive). Either side can qualify —
   including buying the cheap side when the crowd overshoots.
3. **Hold to resolution**: winners redeem at $1. No exit spread, no exit
   slippage, no stop-loss shaken out by noise. Losing entries lose the
   stake — sizing and daily caps are the risk control.
4. Optional `entry_style: maker`: rest a bid inside the spread, capped at
   model-fair minus edge, so a fill both keeps the edge and earns the
   spread. May not fill; the runner rescans for the next signal.

### momentum (legacy, `--mode momentum`)
Buy the leading side when ask ≥ threshold and the BTC spot impulse agrees
(≥ $70 same direction), sell ~20s before close, stop-loss at −25%/−30%.
Kept for comparison; structurally pays the spread twice per trade.

### Shared guards (both modes)
Entry window 180–60s before close, max spread, min top-of-book notional,
quote staleness ≤ 8s, `max_entry_price` 0.95, daily loss/trade caps
(`runtime/daily_state.json`, live mode), kill switch after 5 consecutive
API errors (closes any open position first).

## Run
Dry-run (simulates fills/PnL at real market prices):
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
scripts/btc5m_ctl.sh stop      # graceful: settles open position first
```
Useful overrides (forwarded to the runner): `--mode value|momentum`,
`--entry-style taker|maker`, `--edge-min N`, `--stake-usd N`,
`--entry-timeout-min N`, `--poll-sec N`.

## Measuring edge (do this BEFORE live trading)
```bash
# 1) collect a dataset for a day or more (no orders placed)
.venv/bin/python scripts/btc5m_collect.py --hours 24

# 2) evaluate calibration and simulated PnL
.venv/bin/python scripts/btc5m_eval.py
```
Interpretation:
- `brier_score` well below 0.25 and calibration buckets where `model_avg`
  ≈ `actual_up_rate` → the model is informative.
- `value_rule_grid`: an edge exists only if `avg_pnl_per_$1` is positive
  across *neighboring* thresholds/checkpoints with ≥30 trades each. One
  positive cell is noise. If the grid is flat or negative, tell the user
  the honest answer: no edge, don't trade live.

## Runtime behavior
- One invocation = one trade, then the process exits with a final indented
  JSON report (compact JSON lines before it are heartbeats/events).
- In hold mode the session lasts until market resolution (~up to 10 min).
- Live winnings settle via market redemption — the report includes a
  `redeem_note`; verify the USDC credit in the Polymarket balance.
- Do not run two sessions concurrently — the pidfile prevents it.
- SIGTERM/SIGINT with an open position triggers a real sell before exit.

## Troubleshooting
- `no_entry_timeout`: normal — no qualifying mispricing inside the window.
  With `edge_min: 0.05` most slots produce no trade; that is the point.
- `blocked_by_daily_caps`: daily risk limits reached; do not override
  without explicit user instruction.
- `skip_model_feed_unavailable`: BTC price feeds unreachable; the runner
  never enters blind. Repeated occurrences trip the kill switch.
- `resolution_timeout`: market didn't resolve within the timeout; the
  position is still in the wallet — check Polymarket directly.
- Close retries/fallbacks (`fak` → `gtc` → `force_gtc`) are recorded in
  `close_debug` inside the final report (sell mode).
