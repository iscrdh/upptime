# BTC 5m Polymarket — Claude Code skill

Claude-optimized port of [Novals83/5min-btc-polymarket](https://github.com/Novals83/5min-btc-polymarket)
(originally an OpenClaw skill), rebuilt to be self-contained, to actually
enforce every guard the strategy describes — and extended with a **value
mode** that fixes the economics of the original momentum design.

## Why value mode (the default)

The original strategy buys the leading side at ≥0.70 and sells ~20s before
close. That pays the spread twice per trade and buys "because the price is
high" — which is not an edge. Value mode instead:

1. **Prices the market itself**: `P(up) = Φ(move / (σ₁ₘ√(τ/60)))` from the
   observed BTC move, time remaining and realized 1-minute volatility.
2. **Enters only on mispricing**: model probability must exceed the ask by
   `edge_min` after a fee buffer — on either side, including the cheap one.
3. **Holds to resolution**: winners redeem at $1, so there is no exit
   spread/slippage and no stop-loss triggered by noise. Expected cost per
   trade drops from two crossings to at most one (zero with maker entry).
4. **Measures instead of promising**: `scripts/btc5m_collect.py` records
   model vs. outcome for every 5m slot without placing orders, and
   `scripts/btc5m_eval.py` reports calibration (Brier score) and simulated
   PnL across edge thresholds. If the grid isn't consistently positive,
   there is no edge and the honest move is not to trade.

None of this guarantees profit — counterparties in these markets are fast
bots. It guarantees you pay less per trade, only act when a model says the
price is wrong, and can verify empirically whether that model is right
before risking money.

## What changed vs. the original

| Area | Original (OpenClaw) | This port (Claude Code) |
|---|---|---|
| Order execution | Delegated to a **private** repo (`pm-hl-conservative-plus-repo`) — unusable without it | Direct CLOB orders via `py-clob-client`; fully self-contained |
| BTC impulse ($70–100 move) | Described in docs, **never checked** by code | Enforced: Binance/Coinbase spot move since interval open must agree with the trade direction |
| Entry timing | Any time with ≥60s left | Real entry window (150–60s left, matching the "~2 min" strategy) |
| Spread / liquidity guards | Declared in YAML, ignored (runner even set `PM_MAX_SPREAD=1`) | Enforced per entry (max spread, min top-ask notional, quote staleness) |
| Daily loss cap / max trades | Declared, ignored | Enforced via `runtime/daily_state.json` (UTC day) |
| Kill switch on API errors | Declared, ignored | Enforced (5 consecutive errors → abort, closing any open position) |
| Micro-hedge on extreme skew | Described, never implemented | Implemented (small opposite buy at ≥0.95 skew, ≤45s left) |
| Profiles | Duplicated in YAML **and** hardcoded in Python (values disagreed) | YAML is the single source of truth; CLI flags override per run |
| `ctl start` | Always passed `--execute` (live!) | Dry-run by default; `--execute` must be explicit |
| Stop behavior | `kill` → open position abandoned | SIGTERM traps → position closed before exit; 45s grace before SIGKILL |
| Dry-run | Printed intent only | Simulates fills and PnL at real market prices |
| Crash resilience | Single JSON printed only at the very end | Streams JSON event lines during the run + final report |
| OpenClaw-isms | Cron topics, hot commands, workspace layout | Replaced with Claude Code skill conventions (`SKILL.md` operating rules) |

## Install

Copy this directory to `.claude/skills/btc-5m-polymarket/` in any repo (or
`~/.claude/skills/` for global use), then:

```bash
cd .claude/skills/btc-5m-polymarket
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Requires Python 3.10+.

## Quick start

```bash
# 0) Recommended first: collect data for a day and check if there IS an edge
.venv/bin/python scripts/btc5m_collect.py --hours 24
.venv/bin/python scripts/btc5m_eval.py

# Safe dry-run (simulated fills, real market data; value mode by default)
scripts/btc5m_ctl.sh start --profile conservative
scripts/btc5m_ctl.sh logs
scripts/btc5m_ctl.sh report

# Legacy momentum mode, maker entry, custom edge — all overridable
scripts/btc5m_ctl.sh start --profile aggressive --mode momentum
scripts/btc5m_ctl.sh start --profile conservative --entry-style maker --edge-min 0.07

# Live (requires .env with PM_PRIVATE_KEY / PM_FUNDER; see SKILL.md)
scripts/btc5m_ctl.sh start --profile conservative --execute
```

In a Claude Code session, just ask: *"run a dry-run of the BTC 5m bot"* or
*"give me the PnL report"* — the skill's `SKILL.md` teaches Claude the
commands and the safety rules (live execution only on explicit request).

## Risk notice

Educational/operational infrastructure, not financial advice. Prediction
market trading can lose your entire stake in minutes. Keep stakes small, keep
the daily caps on, and validate any parameter change in dry-run first.
