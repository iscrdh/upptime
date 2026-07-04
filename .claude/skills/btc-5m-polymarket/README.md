# BTC 5m Polymarket — Claude Code skill

Claude-optimized port of [Novals83/5min-btc-polymarket](https://github.com/Novals83/5min-btc-polymarket)
(originally an OpenClaw skill). Same strategy — momentum into close on
Polymarket's BTC 5-minute Up/Down markets — rebuilt to be self-contained and
to actually enforce every guard the strategy describes.

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
# Safe dry-run (simulated fills, real market data)
scripts/btc5m_ctl.sh start --profile conservative
scripts/btc5m_ctl.sh logs
scripts/btc5m_ctl.sh report

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
