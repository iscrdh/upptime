#!/usr/bin/env python3
"""Self-contained BTC 5m Up/Down runner for Polymarket.

Two strategy modes (config `shared.strategy.mode` or --mode):

- value (default): estimate the true probability that BTC finishes the 5m
  interval up, from the observed move, time remaining and recent realized
  volatility (Brownian-motion closed form). Enter only when the market ask
  is cheaper than the model probability by at least `edge_min` (after a fee
  buffer), on either side. Hold to resolution — winning shares pay $1, so
  there is no exit spread/slippage and no noise-triggered stop-loss.
- momentum (legacy): buy the leading side when its ask >= threshold and the
  BTC spot impulse agrees, then sell shortly before close with a stop-loss.

Entry can be `taker` (marketable FAK) or `maker` (rest a GTC bid inside the
spread, capped at model-fair minus edge, so any fill keeps the required
edge and earns the spread instead of paying it).

One invocation = one trade session. Dry-run is the default; live orders
require --execute and Polymarket credentials in the environment
(PM_PRIVATE_KEY, PM_FUNDER, optionally PM_API_KEY/PM_API_SECRET/
PM_API_PASSPHRASE — derived automatically when absent).

Output: compact JSON event lines while running, one indented JSON report at
the end (parsed by scripts/btc5m_report.py).
"""

import argparse
import datetime as dt
import json
import math
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests
import yaml

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

UTC = dt.timezone.utc
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SKILL_ROOT / 'config' / 'btc_5m_profiles.yaml'
DEFAULT_RUNTIME = SKILL_ROOT / 'runtime'

BUY_SLIPPAGE = 0.02   # marketable-limit buffer over best ask when buying
SELL_SLIPPAGE = 0.02  # buffer under best bid when closing with FAK
FAK = getattr(OrderType, 'FAK', OrderType.FOK)


class GracefulExit(Exception):
    pass


def _signal_handler(signum, _frame):
    raise GracefulExit(f'signal_{signum}')


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def ts_utc() -> str:
    return now_utc().isoformat().replace('+00:00', 'Z')


def emit(event: dict[str, Any]) -> None:
    """Stream one compact JSON event line so progress survives crashes."""
    event.setdefault('ts', ts_utc())
    print(json.dumps(event, ensure_ascii=False), flush=True)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def bucket_5m(ts: int) -> int:
    return ts - (ts % 300)


def parse_json_field(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: Path, profile: str) -> dict[str, Any]:
    """Merge shared rules + selected profile into one flat settings dict."""
    raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    shared = raw.get('shared') or {}
    profiles = raw.get('profiles') or {}
    if profile not in profiles:
        raise SystemExit(f'unknown profile {profile!r}; available: {sorted(profiles)}')
    p = profiles[profile] or {}

    def sec(d, k):
        return (d.get(k) or {})

    value = dict(sec(shared, 'value'))
    value.update(sec(p, 'value'))

    cfg = {
        'gamma_base': sec(shared, 'endpoints').get('gamma', 'https://gamma-api.polymarket.com'),
        'clob_base': sec(shared, 'endpoints').get('clob', 'https://clob.polymarket.com'),
        'require_end_in_future_sec_min': sec(shared, 'market_validation').get('require_end_in_future_sec_min', 5),
        'quote_stale_sec_max': sec(shared, 'execution_safety').get('skip_if_quote_stale_sec_gt', 8),
        'kill_switch_errors': sec(shared, 'execution_safety').get('kill_switch_consecutive_api_errors', 5),
        'min_entry_seconds_left': sec(shared, 'entry_window').get('min_entry_seconds_left', 60),
        'max_entry_seconds_left': sec(shared, 'entry_window').get('max_entry_seconds_left', 180),
        'exit_before_sec': sec(shared, 'session').get('exit_before_sec', 20),
        'poll_sec': sec(shared, 'session').get('poll_sec', 5),
        'entry_timeout_min': sec(shared, 'session').get('entry_timeout_min', 60),
        'mode': sec(shared, 'strategy').get('mode', 'value'),
        'entry_style': sec(shared, 'strategy').get('entry_style', 'taker'),
        'exit_style': sec(shared, 'strategy').get('exit_style'),  # None -> by mode
        'edge_min': value.get('edge_min', 0.05),
        'fee_buffer': value.get('fee_buffer', 0.01),
        'vol_lookback_min': value.get('vol_lookback_min', 60),
        'max_entry_price': value.get('max_entry_price', 0.95),
        'resolution_timeout_sec': value.get('resolution_timeout_sec', 300),
        'require_btc_impulse': sec(shared, 'impulse').get('require_btc_impulse', True),
        'btc_move_usd_min': sec(shared, 'impulse').get('btc_move_usd_min', 70),
        'threshold': sec(p, 'signal').get('threshold_price', 0.70),
        'max_spread': sec(p, 'guards').get('max_spread', 0.03),
        'min_top_ask_notional_usd': sec(p, 'guards').get('min_top_ask_notional_usd', 30),
        'stake_usd': sec(p, 'sizing').get('stake_usd', 5),
        'max_notional_usd': sec(p, 'sizing').get('max_notional_usd', 8),
        'daily_max_loss_usd': sec(p, 'sizing').get('daily_max_loss_usd', 15),
        'max_trades_per_day': sec(p, 'sizing').get('max_trades_per_day', 12),
        'hedge': sec(p, 'hedge'),
        'stop_loss_pct': sec(p, 'stop_loss').get('stop_loss_pct_from_entry', 0.25),
    }
    return cfg


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

class MarketData:
    """Gamma + CLOB reads through one shared public client."""

    def __init__(self, gamma_base: str, clob_base: str):
        self.gamma_base = gamma_base.rstrip('/')
        self.clob_base = clob_base.rstrip('/')
        self.pub = ClobClient(host=self.clob_base, chain_id=POLYGON)
        self.http = requests.Session()

    def fetch_event(self, slug: str) -> Optional[dict[str, Any]]:
        r = self.http.get(f'{self.gamma_base}/events', params={'slug': slug}, timeout=12)
        r.raise_for_status()
        arr = r.json()
        return arr[0] if arr else None

    def resolve_current_5m_market(self, min_future_sec: float) -> Optional[dict[str, Any]]:
        slug = f'btc-updown-5m-{bucket_5m(int(time.time()))}'
        ev = self.fetch_event(slug)
        if not ev:
            return None
        mkts = ev.get('markets') or []
        if not mkts:
            return None
        m = mkts[0]
        if m.get('closed') is True or m.get('active') is False:
            return None
        end_iso = str(m.get('endDate') or m.get('endDateIso') or '')
        try:
            end_ts = dt.datetime.fromisoformat(end_iso.replace('Z', '+00:00')).timestamp()
        except Exception:
            return None
        if end_ts - time.time() <= min_future_sec:
            return None
        mm = dict(m)
        mm['_event_slug'] = slug
        mm['_end_ts'] = end_ts
        return mm

    @staticmethod
    def up_index(outcomes) -> int:
        labs = [str(x).lower() for x in (outcomes or [])[:2]]
        if len(labs) >= 2 and ('up' in labs[1] or 'yes' in labs[1]):
            return 1
        return 0

    @staticmethod
    def side_tokens(market: dict[str, Any]) -> tuple[str, str]:
        outcomes = parse_json_field(market.get('outcomes')) or []
        token_ids = parse_json_field(market.get('clobTokenIds')) or []
        if len(token_ids) < 2:
            raise RuntimeError('missing clobTokenIds')
        up_i = MarketData.up_index(outcomes)
        return str(token_ids[up_i]), str(token_ids[1 - up_i])

    def book_top(self, token_id: str) -> dict[str, Any]:
        """Best bid/ask with sizes plus quote age in seconds (when available)."""
        book = self.pub.get_order_book(str(token_id))
        best_bid = best_ask = None
        bid_size = ask_size = 0.0
        for b in getattr(book, 'bids', []) or []:
            p = float(getattr(b, 'price', 0) or 0)
            if best_bid is None or p > best_bid:
                best_bid, bid_size = p, float(getattr(b, 'size', 0) or 0)
        for a in getattr(book, 'asks', []) or []:
            p = float(getattr(a, 'price', 0) or 0)
            if best_ask is None or p < best_ask:
                best_ask, ask_size = p, float(getattr(a, 'size', 0) or 0)
        age_sec = None
        try:
            ts_ms = float(getattr(book, 'timestamp', 0) or 0)
            if ts_ms > 0:
                age_sec = max(0.0, time.time() - ts_ms / 1000.0)
        except Exception:
            age_sec = None
        spread = None
        if best_bid is not None and best_ask is not None:
            spread = max(0.0, best_ask - best_bid)
        return {
            'bid': best_bid, 'bid_size': bid_size,
            'ask': best_ask, 'ask_size': ask_size,
            'spread': spread, 'age_sec': age_sec,
        }


def btc_impulse_usd(bucket_start: int, http: requests.Session) -> Optional[dict[str, Any]]:
    """BTC spot move (USD) since the 5m interval opened; None if all feeds fail."""
    try:
        r = http.get(
            'https://api.binance.com/api/v3/klines',
            params={'symbol': 'BTCUSDT', 'interval': '1m',
                    'startTime': bucket_start * 1000, 'limit': 6},
            timeout=6,
        )
        r.raise_for_status()
        k = r.json()
        if k:
            open_p = float(k[0][1])
            cur = float(k[-1][4])
            return {'open': open_p, 'now': cur, 'move': cur - open_p, 'source': 'binance'}
    except Exception:
        pass
    try:
        start_iso = dt.datetime.fromtimestamp(bucket_start, UTC).isoformat()
        end_iso = now_utc().isoformat()
        r = http.get(
            'https://api.exchange.coinbase.com/products/BTC-USD/candles',
            params={'granularity': 60, 'start': start_iso, 'end': end_iso},
            timeout=6,
        )
        r.raise_for_status()
        rows = r.json()  # newest first: [time, low, high, open, close, volume]
        if rows:
            rows = sorted(rows, key=lambda x: x[0])
            open_p = float(rows[0][3])
            cur = float(rows[-1][4])
            return {'open': open_p, 'now': cur, 'move': cur - open_p, 'source': 'coinbase'}
    except Exception:
        pass
    return None


def estimate_vol_1m_usd(http: requests.Session, lookback_min: int = 60) -> Optional[float]:
    """Zero-drift RMS of 1-minute BTC price changes (USD) over the lookback."""
    closes: list[float] = []
    try:
        r = http.get(
            'https://api.binance.com/api/v3/klines',
            params={'symbol': 'BTCUSDT', 'interval': '1m', 'limit': lookback_min + 1},
            timeout=6,
        )
        r.raise_for_status()
        closes = [float(k[4]) for k in r.json()]
    except Exception:
        try:
            end = now_utc()
            start = end - dt.timedelta(minutes=lookback_min + 1)
            r = http.get(
                'https://api.exchange.coinbase.com/products/BTC-USD/candles',
                params={'granularity': 60, 'start': start.isoformat(), 'end': end.isoformat()},
                timeout=6,
            )
            r.raise_for_status()
            rows = sorted(r.json(), key=lambda x: x[0])
            closes = [float(row[4]) for row in rows]
        except Exception:
            return None
    if len(closes) < 10:
        return None
    diffs = [b - a for a, b in zip(closes, closes[1:])]
    rms = math.sqrt(sum(d * d for d in diffs) / len(diffs))
    return max(rms, 1e-6)


def model_prob_up(move_usd: float, vol_1m_usd: float, seconds_left: float) -> float:
    """P(BTC finishes the interval above its open) under driftless Brownian motion."""
    sigma_tau = max(1e-9, vol_1m_usd * math.sqrt(max(1.0, seconds_left) / 60.0))
    return norm_cdf(move_usd / sigma_tau)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class Executor:
    """Direct CLOB order placement (no external trading repo needed)."""

    def __init__(self, clob_base: str, execute: bool):
        self.execute = execute
        self.client: Optional[ClobClient] = None
        if not execute:
            return
        key = os.getenv('PM_PRIVATE_KEY') or ''
        if not key:
            raise SystemExit('--execute requires PM_PRIVATE_KEY in the environment')
        funder = os.getenv('PM_FUNDER') or os.getenv('PM_ADDRESS') or None
        sig = int(os.getenv('PM_SIGNATURE_TYPE', '2'))
        c = ClobClient(host=clob_base, chain_id=POLYGON, key=key,
                       signature_type=sig, funder=funder)
        v1 = os.getenv('PM_API_KEY') or ''
        v2 = os.getenv('PM_API_SECRET') or ''
        v3 = os.getenv('PM_API_PASSPHRASE') or ''
        if v1 and v2 and v3:
            c.set_api_creds(ApiCreds(api_key=v1, api_secret=v2, api_passphrase=v3))
        else:
            c.set_api_creds(c.create_or_derive_api_creds())
        self.client = c

    def _post(self, token_id: str, price: float, size: float, side: str,
              order_type) -> dict[str, Any]:
        price = round(clamp(price, 0.01, 0.99), 2)
        size = round(size, 2)
        if size <= 0:
            return {'success': False, 'errorMsg': 'zero_size'}
        if not self.execute:
            return {
                'success': True, 'status': 'simulated', 'orderID': None,
                'simulated': {'token_id': token_id, 'price': price,
                              'size': size, 'side': side,
                              'order_type': str(order_type)},
            }
        signed = self.client.create_order(
            OrderArgs(price=price, size=size, side=side, token_id=str(token_id)))
        return self.client.post_order(signed, order_type)

    def marketable_buy(self, token_id: str, best_ask: float, amount_usd: float) -> dict[str, Any]:
        price = clamp(best_ask + BUY_SLIPPAGE, 0.01, 0.99)
        return self._post(token_id, price, amount_usd / price, BUY, FAK)

    def gtc_buy(self, token_id: str, price: float, size: float) -> dict[str, Any]:
        return self._post(token_id, price, size, BUY, OrderType.GTC)

    def fak_sell(self, token_id: str, best_bid: float, shares: float) -> dict[str, Any]:
        price = clamp(best_bid - SELL_SLIPPAGE, 0.01, 0.99)
        return self._post(token_id, price, shares, SELL, FAK)

    def limit_sell(self, token_id: str, price: float, shares: float) -> dict[str, Any]:
        return self._post(token_id, price, shares, SELL, OrderType.GTC)

    def get_order_info(self, order_id: str) -> dict[str, Any]:
        if not self.execute or not order_id:
            return {}
        try:
            return self.client.get_order(order_id) or {}
        except Exception:
            return {}

    def order_status(self, order_id: str, wait_sec: float, step_sec: float = 1.0) -> str:
        if not self.execute or not order_id:
            return ''
        deadline = time.time() + max(0.0, wait_sec)
        st = ''
        while time.time() <= deadline:
            st = str(self.get_order_info(order_id).get('status') or '').upper()
            if st and st not in ('LIVE', 'OPEN'):
                return st
            time.sleep(max(0.2, step_sec))
        return st

    def cancel_order(self, order_id: str) -> Optional[dict[str, Any]]:
        if not self.execute or not order_id:
            return None
        try:
            return self.client.cancel(order_id=order_id)
        except Exception as e:
            return {'error': str(e)}

    def cancel_token_orders(self, token_id: str) -> Optional[dict[str, Any]]:
        if not self.execute:
            return None
        try:
            return self.client.cancel_market_orders(asset_id=str(token_id))
        except Exception as e:
            return {'error': str(e)}


def post_amounts(post: dict[str, Any], side: str) -> tuple[float, float]:
    """(shares, usdc) actually filled. BUY: taking=shares/making=usdc; SELL inverse."""
    making = float(post.get('makingAmount') or 0)
    taking = float(post.get('takingAmount') or 0)
    return (taking, making) if side == BUY else (making, taking)


def is_matched(post: dict[str, Any]) -> bool:
    return bool(post.get('success')) and str(post.get('status') or '').lower() in ('matched', 'simulated')


# ---------------------------------------------------------------------------
# Daily risk state
# ---------------------------------------------------------------------------

class DailyState:
    def __init__(self, runtime_dir: Path, enabled: bool):
        self.path = runtime_dir / 'daily_state.json'
        self.enabled = enabled
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        today = now_utc().date().isoformat()
        try:
            s = json.loads(self.path.read_text(encoding='utf-8'))
            if s.get('date') == today:
                return s
        except Exception:
            pass
        return {'date': today, 'trades': 0, 'realized_pnl_usdc': 0.0}

    def check_caps(self, max_trades: int, max_loss_usd: float) -> Optional[str]:
        if self.state['trades'] >= max_trades:
            return f"daily_max_trades_reached({self.state['trades']}/{max_trades})"
        if self.state['realized_pnl_usdc'] <= -abs(max_loss_usd):
            return f"daily_max_loss_reached({self.state['realized_pnl_usdc']:.2f} <= -{abs(max_loss_usd):.2f})"
        return None

    def record_trade(self, pnl: Optional[float]) -> None:
        if not self.enabled:
            return
        self.state['trades'] += 1
        if isinstance(pnl, (int, float)):
            self.state['realized_pnl_usdc'] = round(self.state['realized_pnl_usdc'] + float(pnl), 6)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2), encoding='utf-8')


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--config', default=str(DEFAULT_CONFIG))
    ap.add_argument('--profile', choices=['conservative', 'aggressive'], default='conservative')
    ap.add_argument('--runtime-dir', default=str(DEFAULT_RUNTIME))
    ap.add_argument('--mode', choices=['value', 'momentum'], default=None)
    ap.add_argument('--entry-style', choices=['taker', 'maker'], default=None)
    ap.add_argument('--exit-style', choices=['hold', 'sell'], default=None)
    ap.add_argument('--edge-min', type=float, default=None, help='value mode: min model edge over ask (after fee buffer)')
    ap.add_argument('--threshold', type=float, default=None)
    ap.add_argument('--stake-usd', type=float, default=None)
    ap.add_argument('--stop-loss-pct', type=float, default=None, help='0.25 = close at -25%% from entry price (sell mode)')
    ap.add_argument('--exit-before-sec', type=int, default=None)
    ap.add_argument('--min-entry-seconds-left', type=int, default=None)
    ap.add_argument('--max-entry-seconds-left', type=int, default=None)
    ap.add_argument('--entry-timeout-min', type=int, default=None)
    ap.add_argument('--poll-sec', type=float, default=None)
    ap.add_argument('--btc-move-usd', type=float, default=None, help='momentum mode: min BTC spot move')
    ap.add_argument('--no-impulse-check', action='store_true', help='momentum mode: disable BTC impulse confirmation')
    ap.add_argument('--close-retry-max', type=int, default=18)
    ap.add_argument('--close-retry-delay-sec', type=float, default=2.0)
    ap.add_argument('--execute', action='store_true', help='place real orders (default: dry-run)')
    return ap.parse_args()


def build_settings(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config(Path(args.config), args.profile)
    overrides = {
        'mode': args.mode,
        'entry_style': args.entry_style,
        'exit_style': args.exit_style,
        'edge_min': args.edge_min,
        'threshold': args.threshold,
        'stake_usd': args.stake_usd,
        'stop_loss_pct': args.stop_loss_pct,
        'exit_before_sec': args.exit_before_sec,
        'min_entry_seconds_left': args.min_entry_seconds_left,
        'max_entry_seconds_left': args.max_entry_seconds_left,
        'entry_timeout_min': args.entry_timeout_min,
        'poll_sec': args.poll_sec,
        'btc_move_usd_min': args.btc_move_usd,
    }
    for k, v in overrides.items():
        if v is not None:
            cfg[k] = v
    if args.no_impulse_check:
        cfg['require_btc_impulse'] = False
    if not cfg.get('exit_style'):
        cfg['exit_style'] = 'hold' if cfg['mode'] == 'value' else 'sell'
    cfg['stake_usd'] = min(float(cfg['stake_usd']), float(cfg['max_notional_usd']))
    return cfg


def find_entry(md: MarketData, cfg: dict[str, Any], deadline: float,
               poll_sec: float, kill_switch: int) -> Optional[dict[str, Any]]:
    """Poll until one side qualifies. Returns entry decision dict or None on timeout."""
    api_errors = 0
    vol_cache = {'t': 0.0, 'v': None}
    while time.time() < deadline:
        try:
            m = md.resolve_current_5m_market(cfg['require_end_in_future_sec_min'])
            if not m:
                emit({'status': 'heartbeat_no_current_market'})
                time.sleep(poll_sec)
                continue
            slug = m['_event_slug']
            sec_left = max(0.0, m['_end_ts'] - time.time())

            if sec_left < cfg['min_entry_seconds_left']:
                emit({'status': 'skip_too_late_to_enter', 'slug': slug, 'seconds_left': round(sec_left, 1)})
                time.sleep(poll_sec)
                continue
            if sec_left > cfg['max_entry_seconds_left']:
                emit({'status': 'waiting_entry_window', 'slug': slug, 'seconds_left': round(sec_left, 1)})
                time.sleep(min(poll_sec, max(1.0, sec_left - cfg['max_entry_seconds_left'])))
                continue

            up_t, dn_t = md.side_tokens(m)
            up = md.book_top(up_t)
            dn = md.book_top(dn_t)
            api_errors = 0

            emit({'status': 'heartbeat', 'slug': slug, 'seconds_left': round(sec_left, 1),
                  'up_ask': up['ask'], 'down_ask': dn['ask'],
                  'up_spread': up['spread'], 'down_spread': dn['spread']})

            stale_max = cfg['quote_stale_sec_max']
            for name, side_book in (('UP', up), ('DOWN', dn)):
                if side_book['age_sec'] is not None and side_book['age_sec'] > stale_max:
                    emit({'status': 'skip_quote_stale', 'side': name, 'age_sec': round(side_book['age_sec'], 1)})
                    side_book['ask'] = None  # disqualify this side for this poll

            model = None
            candidates: list[tuple[str, str, dict, float]] = []  # side, token, book, rank

            if cfg['mode'] == 'value':
                impulse = btc_impulse_usd(int(m['_end_ts']) - 300, md.http)
                if time.time() - vol_cache['t'] > 60:
                    vol_cache['v'] = estimate_vol_1m_usd(md.http, cfg['vol_lookback_min'])
                    vol_cache['t'] = time.time()
                vol = vol_cache['v']
                if impulse is None or vol is None:
                    api_errors += 1
                    emit({'status': 'skip_model_feed_unavailable', 'api_errors': api_errors})
                    if api_errors >= kill_switch:
                        raise GracefulExit('kill_switch_api_errors')
                    time.sleep(poll_sec)
                    continue
                p_up = model_prob_up(impulse['move'], vol, sec_left)
                model = {'p_up': round(p_up, 4), 'btc_move_usd': round(impulse['move'], 2),
                         'vol_1m_usd': round(vol, 2), 'seconds_left': round(sec_left, 1),
                         'source': impulse['source']}
                for side, token, book, fair in (('UP', up_t, up, p_up), ('DOWN', dn_t, dn, 1.0 - p_up)):
                    ask = book['ask']
                    if ask is None or ask > cfg['max_entry_price']:
                        continue
                    edge = fair - ask - cfg['fee_buffer']
                    if edge >= cfg['edge_min']:
                        candidates.append((side, token, book, edge))
                if not candidates:
                    emit({'status': 'skip_no_edge', **model,
                          'up_ask': up['ask'], 'down_ask': dn['ask'],
                          'edge_min': cfg['edge_min']})
                    time.sleep(poll_sec)
                    continue
            else:  # momentum (legacy)
                if up['ask'] is not None and up['ask'] >= cfg['threshold']:
                    candidates.append(('UP', up_t, up, up['ask']))
                if dn['ask'] is not None and dn['ask'] >= cfg['threshold']:
                    candidates.append(('DOWN', dn_t, dn, dn['ask']))
                if not candidates:
                    time.sleep(poll_sec)
                    continue
                if cfg['require_btc_impulse']:
                    impulse = btc_impulse_usd(int(m['_end_ts']) - 300, md.http)
                    if impulse is None:
                        api_errors += 1
                        emit({'status': 'skip_impulse_feed_unavailable', 'api_errors': api_errors})
                        if api_errors >= kill_switch:
                            raise GracefulExit('kill_switch_api_errors')
                        time.sleep(poll_sec)
                        continue
                    move = impulse['move']
                    model = {'btc_move_usd': round(move, 2), 'source': impulse['source']}
                    candidates = [
                        c for c in candidates
                        if (c[0] == 'UP' and move >= cfg['btc_move_usd_min'])
                        or (c[0] == 'DOWN' and move <= -cfg['btc_move_usd_min'])
                    ]
                    if not candidates:
                        emit({'status': 'skip_no_impulse_agreement', 'btc_move_usd': round(move, 2),
                              'required_usd': cfg['btc_move_usd_min'], 'source': impulse['source']})
                        time.sleep(poll_sec)
                        continue

            side, token, book, rank = max(candidates, key=lambda c: c[3])

            if book['spread'] is not None and book['spread'] > cfg['max_spread']:
                emit({'status': 'skip_spread_too_wide', 'side': side, 'spread': round(book['spread'], 4)})
                time.sleep(poll_sec)
                continue
            top_notional = (book['ask'] or 0) * (book['ask_size'] or 0)
            if top_notional < cfg['min_top_ask_notional_usd']:
                emit({'status': 'skip_thin_liquidity', 'side': side,
                      'top_ask_notional_usd': round(top_notional, 2)})
                time.sleep(poll_sec)
                continue

            entry_model = None
            if model is not None:
                entry_model = dict(model)
                if cfg['mode'] == 'value':
                    entry_model['edge'] = round(rank, 4)
                    entry_model['fair'] = round(model['p_up'] if side == 'UP' else 1.0 - model['p_up'], 4)
            return {
                'market': m, 'slug': slug, 'side': side, 'token_id': token,
                'opp_token_id': dn_t if side == 'UP' else up_t,
                'best_ask': book['ask'], 'book': book, 'seconds_left': sec_left,
                'model': entry_model,
            }
        except GracefulExit:
            raise
        except Exception as e:
            api_errors += 1
            emit({'status': 'error', 'error': str(e), 'api_errors': api_errors})
            if api_errors >= kill_switch:
                raise GracefulExit('kill_switch_api_errors')
        time.sleep(poll_sec)
    return None


def maker_entry(ex: Executor, md: MarketData, cfg: dict[str, Any],
                entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Rest a GTC bid inside the spread instead of crossing it.

    The bid is capped at model-fair minus edge_min (value mode), so a fill can
    only happen at a price that keeps the required edge. Returns fill info or
    None if unfilled when the entry window closes.
    """
    token = entry['token_id']
    end_ts = entry['market']['_end_ts']
    fair = (entry.get('model') or {}).get('fair')
    cap = (fair - cfg['edge_min'] - cfg['fee_buffer']) if fair else entry['best_ask']
    bid = entry['book']['bid'] or 0.01
    limit_px = round(clamp(min(bid + 0.01, cap, entry['best_ask']), 0.01, 0.99), 2)
    if limit_px < 0.02:
        return None
    size = round(cfg['stake_usd'] / limit_px, 2)
    emit({'status': 'maker_bid_posted', 'side': entry['side'], 'price': limit_px, 'size': size})

    order_id = None
    if ex.execute:
        post = ex.gtc_buy(token, limit_px, size)
        if is_matched(post):
            shares, cost = post_amounts(post, BUY)
            return {'shares': shares, 'cost': cost, 'order_id': post.get('orderID'),
                    'tx': (post.get('transactionsHashes') or [None])[0], 'price': limit_px,
                    'style': 'maker_immediate'}
        if not post.get('success'):
            emit({'status': 'maker_post_failed', 'error': str(post.get('errorMsg') or '')[:200]})
            return None
        order_id = str(post.get('orderID') or '')

    poll = max(1.0, min(cfg['poll_sec'], 3.0))
    while end_ts - time.time() > cfg['min_entry_seconds_left']:
        if ex.execute:
            info = ex.get_order_info(order_id)
            st = str(info.get('status') or '').upper()
            if st == 'MATCHED':
                shares = float(info.get('size_matched') or info.get('sizeMatched') or size)
                return {'shares': shares, 'cost': round(shares * limit_px, 6),
                        'order_id': order_id, 'tx': None, 'price': limit_px, 'style': 'maker'}
            if st in ('CANCELED', 'CANCELLED'):
                return None
        else:
            try:
                ask = md.book_top(token)['ask']
            except Exception:
                ask = None
            if ask is not None and ask <= limit_px:
                return {'shares': size, 'cost': round(size * limit_px, 6),
                        'order_id': None, 'tx': None, 'price': limit_px,
                        'style': 'maker_simulated'}
        time.sleep(poll)

    # window closed: cancel and keep whatever was partially filled
    if ex.execute and order_id:
        ex.cancel_order(order_id)
        info = ex.get_order_info(order_id)
        filled = float(info.get('size_matched') or info.get('sizeMatched') or 0)
        if filled > 0:
            return {'shares': filled, 'cost': round(filled * limit_px, 6),
                    'order_id': order_id, 'tx': None, 'price': limit_px,
                    'style': 'maker_partial'}
    emit({'status': 'maker_unfilled', 'price': limit_px})
    return None


# ---------------------------------------------------------------------------
# Exit
# ---------------------------------------------------------------------------

def wait_for_resolution(md: MarketData, cfg: dict[str, Any],
                        opened: dict[str, Any]) -> Optional[str]:
    """Sleep to market end, then poll Gamma until it resolves. 'UP'/'DOWN'/None."""
    end_ts = opened['market_end_ts']
    while end_ts + 3 - time.time() > 0:
        time.sleep(min(30.0, max(0.5, end_ts + 3 - time.time())))
    deadline = end_ts + cfg['resolution_timeout_sec']
    while time.time() < deadline:
        try:
            ev = md.fetch_event(opened['market_slug'])
            mkts = (ev or {}).get('markets') or []
            if mkts:
                m0 = mkts[0]
                prices = parse_json_field(m0.get('outcomePrices')) or []
                outcomes = parse_json_field(m0.get('outcomes')) or []
                if len(prices) >= 2:
                    p = [float(x) for x in prices[:2]]
                    if max(p) >= 0.999 and min(p) <= 0.001:
                        ui = MarketData.up_index(outcomes)
                        winner_i = 0 if p[0] > p[1] else 1
                        return 'UP' if winner_i == ui else 'DOWN'
        except Exception as e:
            emit({'status': 'resolution_poll_error', 'error': str(e)})
        time.sleep(10)
    return None


def maybe_hedge(ex: Executor, md: MarketData, cfg: dict[str, Any],
                opened: dict[str, Any], held_bid: Optional[float],
                sec_left: float, report: dict[str, Any]) -> None:
    h = cfg['hedge'] or {}
    if not h.get('enabled') or report.get('hedge'):
        return
    if held_bid is None or held_bid < float(h.get('trigger_side_price_gte', 0.95)):
        return
    if sec_left > float(h.get('trigger_seconds_left_lte', 45)):
        return
    amount = clamp(
        opened['cost_usdc'] * float(h.get('hedge_share_of_main_pct', 3)) / 100.0,
        float(h.get('hedge_notional_usd_min', 1)),
        float(h.get('hedge_notional_usd_max', 2)),
    )
    try:
        opp = md.book_top(opened['opp_token_id'])
        if opp['ask'] is None:
            return
        post = ex.marketable_buy(opened['opp_token_id'], opp['ask'], amount)
        shares, usdc = post_amounts(post, BUY)
        report['hedge'] = {
            'ts': ts_utc(), 'notional_usd': round(amount, 2), 'opp_ask': opp['ask'],
            'status': post.get('status'), 'order_id': post.get('orderID'),
            'shares': shares, 'cost_usdc': usdc,
        }
        emit({'status': 'hedge_placed', **report['hedge']})
    except Exception as e:
        report['hedge'] = {'ts': ts_utc(), 'error': str(e)}
        emit({'status': 'hedge_failed', 'error': str(e)})


def close_position(ex: Executor, md: MarketData, opened: dict[str, Any],
                   args: argparse.Namespace, report: dict[str, Any]) -> dict[str, Any]:
    """FAK close with retries; GTC-limit fallback; cancel+repost force-close."""
    debug: list[dict[str, Any]] = []
    final_post: dict[str, Any] = {}

    for attempt in range(1, max(1, args.close_retry_max) + 1):
        try:
            bb = md.book_top(opened['token_id'])['bid']
        except Exception:
            bb = None
        if bb is None:
            debug.append({'ts': ts_utc(), 'attempt': attempt, 'step': 'no_bid'})
            time.sleep(args.close_retry_delay_sec)
            continue

        if not ex.execute:
            # dry-run: settle the simulation at the current executable bid
            final_post = {'success': True, 'status': 'simulated',
                          'makingAmount': opened['shares'],
                          'takingAmount': round(opened['shares'] * bb, 6)}
            debug.append({'ts': ts_utc(), 'attempt': attempt, 'step': 'simulated_close', 'bid': bb})
            break

        try:
            post = ex.fak_sell(opened['token_id'], bb, opened['shares'])
        except Exception as e:
            post = {'success': False, 'errorMsg': str(e)}
        status = str(post.get('status') or '').lower()
        err = str(post.get('errorMsg') or '')
        debug.append({'ts': ts_utc(), 'attempt': attempt, 'step': 'fak',
                      'status': status, 'error': err[:200]})
        if is_matched(post):
            final_post = post
            break

        txt = err.lower()
        # position not yet visible on the book right after open — plain retry
        if 'balance' in txt or 'allowance' in txt:
            time.sleep(args.close_retry_delay_sec)
            continue

        # FAK found no takers: rest a GTC just under best bid, then escalate
        if 'no orders found' in txt or 'match' in txt or not post.get('success'):
            limit_px = clamp(bb - 0.01, 0.01, 0.99)
            try:
                post2 = ex.limit_sell(opened['token_id'], limit_px, opened['shares'])
            except Exception as e:
                post2 = {'success': False, 'errorMsg': str(e)}
            status2 = str(post2.get('status') or '').lower()
            debug.append({'ts': ts_utc(), 'attempt': attempt, 'step': 'gtc',
                          'status': status2, 'limit_price': limit_px})
            if is_matched(post2):
                final_post = post2
                break
            if post2.get('success') and status2 == 'live':
                st = ex.order_status(str(post2.get('orderID') or ''), wait_sec=6.0)
                debug.append({'ts': ts_utc(), 'attempt': attempt, 'step': 'gtc_poll', 'status': st.lower()})
                if st == 'MATCHED':
                    post2['status'] = 'matched'
                    final_post = post2
                    break
                cancel_info = ex.cancel_token_orders(opened['token_id'])
                try:
                    bb2 = md.book_top(opened['token_id'])['bid']
                except Exception:
                    bb2 = None
                force_px = clamp((bb2 - 0.02) if bb2 is not None else 0.01, 0.01, 0.99)
                try:
                    post3 = ex.limit_sell(opened['token_id'], force_px, opened['shares'])
                except Exception as e:
                    post3 = {'success': False, 'errorMsg': str(e)}
                status3 = str(post3.get('status') or '').lower()
                debug.append({'ts': ts_utc(), 'attempt': attempt, 'step': 'force_gtc',
                              'status': status3, 'limit_price': force_px,
                              'cancel_info': cancel_info})
                if is_matched(post3):
                    final_post = post3
                    break
        time.sleep(args.close_retry_delay_sec)

    report['close_debug'] = debug
    shares_sold, usdc = post_amounts(final_post, SELL)
    return {
        'closed_at': ts_utc(),
        'close_success': is_matched(final_post) or usdc > 0,
        'close_status': final_post.get('status'),
        'close_order_id': final_post.get('orderID'),
        'close_tx': (final_post.get('transactionsHashes') or [None])[0],
        'close_shares': shares_sold,
        'close_usdc': usdc,
    }


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def main() -> None:
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    args = parse_args()
    cfg = build_settings(args)
    runtime_dir = Path(args.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    md = MarketData(cfg['gamma_base'], cfg['clob_base'])
    ex = Executor(cfg['clob_base'], args.execute)
    daily = DailyState(runtime_dir, enabled=args.execute)

    report: dict[str, Any] = {
        'started_at': ts_utc(),
        'params': {**{k: v for k, v in cfg.items() if k not in ('hedge',)},
                   'profile': args.profile, 'execute': args.execute,
                   'hedge': cfg['hedge']},
    }
    emit({'status': 'session_start', 'profile': args.profile, 'mode': cfg['mode'],
          'entry_style': cfg['entry_style'], 'exit_style': cfg['exit_style'],
          'execute': args.execute, 'stake_usd': cfg['stake_usd']})

    cap_reason = daily.check_caps(cfg['max_trades_per_day'], cfg['daily_max_loss_usd'])
    if cap_reason:
        report.update({'finished_at': ts_utc(), 'result': 'blocked_by_daily_caps',
                       'reason': cap_reason, 'daily_state': daily.state})
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    opened = None
    close_reason = None
    outcome = None
    try:
        deadline = time.time() + cfg['entry_timeout_min'] * 60
        while not opened and time.time() < deadline:
            entry = find_entry(md, cfg, deadline, cfg['poll_sec'], cfg['kill_switch_errors'])
            if not entry:
                break

            if cfg['entry_style'] == 'maker':
                fill = maker_entry(ex, md, cfg, entry)
                if not fill:
                    continue  # window closed unfilled — rescan for the next signal
                shares, cost = fill['shares'], fill['cost']
                open_order_id, open_tx = fill.get('order_id'), fill.get('tx')
            else:
                post = ex.marketable_buy(entry['token_id'], entry['best_ask'], cfg['stake_usd'])
                if not is_matched(post):
                    emit({'status': 'open_failed_retry',
                          'error': str(post.get('errorMsg') or post.get('status') or '')[:200]})
                    time.sleep(cfg['poll_sec'])
                    continue
                shares, cost = post_amounts(post, BUY)
                if not ex.execute:
                    px = entry['best_ask']
                    shares = round(cfg['stake_usd'] / px, 2)
                    cost = round(shares * px, 6)
                open_order_id = post.get('orderID')
                open_tx = (post.get('transactionsHashes') or [None])[0]

            if shares <= 0:
                continue
            opened = {
                'opened_at': ts_utc(),
                'market_slug': entry['slug'],
                'market_end_ts': entry['market']['_end_ts'],
                'side': entry['side'],
                'token_id': entry['token_id'],
                'opp_token_id': entry['opp_token_id'],
                'entry_price': round(cost / shares, 6) if shares else entry['best_ask'],
                'shares': shares,
                'cost_usdc': cost,
                'open_order_id': open_order_id,
                'open_tx': open_tx,
                'entry_style': cfg['entry_style'],
                'model': entry['model'],
            }
            report['opened'] = opened
            emit({'status': 'opened', 'side': opened['side'], 'slug': opened['market_slug'],
                  'entry_price': opened['entry_price'], 'shares': shares,
                  'cost_usdc': cost, 'model': entry['model']})

        if not opened:
            report.update({'finished_at': ts_utc(), 'result': 'no_entry_timeout'})
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return

        if cfg['exit_style'] == 'hold':
            emit({'status': 'holding_to_resolution',
                  'seconds_to_end': round(opened['market_end_ts'] - time.time(), 1)})
            outcome = wait_for_resolution(md, cfg, opened)
            close_reason = 'held_to_resolution'
        else:
            sl_price = opened['entry_price'] * (1.0 - cfg['stop_loss_pct'])
            report['stop_loss_price'] = round(sl_price, 6)
            end_ts = opened['market_end_ts']
            while True:
                sec_left = end_ts - time.time()
                if sec_left <= cfg['exit_before_sec']:
                    close_reason = f"time_exit_{cfg['exit_before_sec']}s_before_end"
                    break
                bid = None
                try:
                    bid = md.book_top(opened['token_id'])['bid']
                except Exception as e:
                    emit({'status': 'monitor_error', 'error': str(e)})
                if bid is not None:
                    report['last_bid'] = bid
                    report['last_check_at'] = ts_utc()
                    if bid <= sl_price:
                        close_reason = f"stop_loss_{int(cfg['stop_loss_pct'] * 100)}pct"
                        break
                    maybe_hedge(ex, md, cfg, opened, bid, sec_left, report)
                time.sleep(cfg['poll_sec'])
    except GracefulExit as e:
        if not opened:
            report.update({'finished_at': ts_utc(), 'result': 'aborted',
                           'reason': str(e)})
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        close_reason = f'aborted_{e}'
        emit({'status': 'abort_with_open_position', 'reason': str(e)})

    # From here on, finish settling even if another stop signal arrives.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    if close_reason == 'held_to_resolution':
        if outcome is None:
            closed = {'closed_at': ts_utc(), 'close_success': False,
                      'close_status': 'resolution_timeout', 'close_usdc': 0.0,
                      'close_shares': 0.0, 'close_order_id': None, 'close_tx': None}
        else:
            won = outcome == opened['side']
            closed = {'closed_at': ts_utc(), 'close_success': True,
                      'close_status': 'resolved', 'resolution': outcome, 'won': won,
                      'close_usdc': round(opened['shares'] * 1.0, 6) if won else 0.0,
                      'close_shares': opened['shares'], 'close_order_id': None, 'close_tx': None}
            if won and ex.execute:
                report['redeem_note'] = ('winning shares settle via market redemption; '
                                         'verify the USDC credit in your Polymarket balance')
            emit({'status': 'resolved', 'outcome': outcome, 'won': won})
    else:
        closed = close_position(ex, md, opened, args, report)
    closed['close_reason'] = close_reason
    report['closed'] = closed

    pnl = None
    if closed['close_usdc'] or closed['close_success']:
        pnl = round(closed['close_usdc'] - opened['cost_usdc'], 6)
        if report.get('hedge', {}).get('cost_usdc'):
            pnl = round(pnl - float(report['hedge']['cost_usdc']), 6)
    report['realized_cashflow_pnl_usdc'] = pnl
    daily.record_trade(pnl)
    report['daily_state'] = daily.state
    report['finished_at'] = ts_utc()
    report['result'] = 'done'
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
