#!/usr/bin/env python3
"""Self-contained BTC 5m Up/Down momentum runner for Polymarket.

One invocation = one trade session:
  1. Wait for the current 5m market to enter the entry window.
  2. Confirm momentum (CLOB ask >= threshold AND real BTC spot impulse in the
     same direction) and pass spread/liquidity/staleness/daily-risk guards.
  3. Open a marketable-limit BUY (FAK) on the stronger side.
  4. Optionally micro-hedge the opposite side on extreme skew.
  5. Monitor for stop-loss, then close before market end (FAK, with GTC
     limit fallback and force-close escalation).

Dry-run is the default; live orders require --execute and Polymarket
credentials in the environment (PM_PRIVATE_KEY, PM_FUNDER, optionally
PM_API_KEY/PM_API_SECRET/PM_API_PASSPHRASE — derived automatically when
absent).

Output: compact JSON event lines while running, one indented JSON report at
the end (parsed by scripts/btc5m_report.py).
"""

import argparse
import datetime as dt
import json
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

    cfg = {
        'gamma_base': sec(shared, 'endpoints').get('gamma', 'https://gamma-api.polymarket.com'),
        'clob_base': sec(shared, 'endpoints').get('clob', 'https://clob.polymarket.com'),
        'require_end_in_future_sec_min': sec(shared, 'market_validation').get('require_end_in_future_sec_min', 5),
        'quote_stale_sec_max': sec(shared, 'execution_safety').get('skip_if_quote_stale_sec_gt', 8),
        'kill_switch_errors': sec(shared, 'execution_safety').get('kill_switch_consecutive_api_errors', 5),
        'min_entry_seconds_left': sec(shared, 'entry_window').get('min_entry_seconds_left', 60),
        'max_entry_seconds_left': sec(shared, 'entry_window').get('max_entry_seconds_left', 150),
        'exit_before_sec': sec(shared, 'session').get('exit_before_sec', 20),
        'poll_sec': sec(shared, 'session').get('poll_sec', 5),
        'entry_timeout_min': sec(shared, 'session').get('entry_timeout_min', 60),
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
    def side_tokens(market: dict[str, Any]) -> tuple[str, str]:
        outcomes = parse_json_field(market.get('outcomes')) or []
        token_ids = parse_json_field(market.get('clobTokenIds')) or []
        if len(token_ids) < 2:
            raise RuntimeError('missing clobTokenIds')
        up_i, down_i = 0, 1
        labs = [str(x).lower() for x in outcomes[:2]] if isinstance(outcomes, list) else []
        if len(labs) >= 2 and ('up' in labs[1] or 'yes' in labs[1]):
            up_i, down_i = 1, 0
        return str(token_ids[up_i]), str(token_ids[down_i])

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


def btc_impulse_usd(bucket_start: int, http: requests.Session) -> Optional[dict[str, float]]:
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

    def fak_sell(self, token_id: str, best_bid: float, shares: float) -> dict[str, Any]:
        price = clamp(best_bid - SELL_SLIPPAGE, 0.01, 0.99)
        return self._post(token_id, price, shares, SELL, FAK)

    def limit_sell(self, token_id: str, price: float, shares: float) -> dict[str, Any]:
        return self._post(token_id, price, shares, SELL, OrderType.GTC)

    def order_status(self, order_id: str, wait_sec: float, step_sec: float = 1.0) -> str:
        if not self.execute or not order_id:
            return ''
        deadline = time.time() + max(0.0, wait_sec)
        st = ''
        while time.time() <= deadline:
            try:
                o = self.client.get_order(order_id)
                st = str((o or {}).get('status') or '').upper()
                if st and st not in ('LIVE', 'OPEN'):
                    return st
            except Exception:
                pass
            time.sleep(max(0.2, step_sec))
        return st

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
# Session
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--config', default=str(DEFAULT_CONFIG))
    ap.add_argument('--profile', choices=['conservative', 'aggressive'], default='conservative')
    ap.add_argument('--runtime-dir', default=str(DEFAULT_RUNTIME))
    ap.add_argument('--threshold', type=float, default=None)
    ap.add_argument('--stake-usd', type=float, default=None)
    ap.add_argument('--stop-loss-pct', type=float, default=None, help='0.25 = close at -25%% from entry price')
    ap.add_argument('--exit-before-sec', type=int, default=None)
    ap.add_argument('--min-entry-seconds-left', type=int, default=None)
    ap.add_argument('--max-entry-seconds-left', type=int, default=None)
    ap.add_argument('--entry-timeout-min', type=int, default=None)
    ap.add_argument('--poll-sec', type=float, default=None)
    ap.add_argument('--btc-move-usd', type=float, default=None, help='min BTC spot move to confirm momentum')
    ap.add_argument('--no-impulse-check', action='store_true', help='disable the BTC spot impulse confirmation')
    ap.add_argument('--close-retry-max', type=int, default=18)
    ap.add_argument('--close-retry-delay-sec', type=float, default=2.0)
    ap.add_argument('--execute', action='store_true', help='place real orders (default: dry-run)')
    return ap.parse_args()


def build_settings(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config(Path(args.config), args.profile)
    overrides = {
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
    cfg['stake_usd'] = min(float(cfg['stake_usd']), float(cfg['max_notional_usd']))
    return cfg


def find_entry(md: MarketData, cfg: dict[str, Any], deadline: float,
               poll_sec: float, kill_switch: int) -> Optional[dict[str, Any]]:
    """Poll until one side qualifies. Returns entry decision dict or None on timeout."""
    api_errors = 0
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

            candidates = []
            if up['ask'] is not None and up['ask'] >= cfg['threshold']:
                candidates.append(('UP', up_t, up))
            if dn['ask'] is not None and dn['ask'] >= cfg['threshold']:
                candidates.append(('DOWN', dn_t, dn))
            if not candidates:
                time.sleep(poll_sec)
                continue

            impulse = None
            if cfg['require_btc_impulse']:
                bucket = int(m['_end_ts']) - 300
                impulse = btc_impulse_usd(bucket, md.http)
                if impulse is None:
                    api_errors += 1
                    emit({'status': 'skip_impulse_feed_unavailable', 'api_errors': api_errors})
                    if api_errors >= kill_switch:
                        raise GracefulExit('kill_switch_api_errors')
                    time.sleep(poll_sec)
                    continue
                move = impulse['move']
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

            side, token, book = max(candidates, key=lambda c: c[2]['ask'])

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

            return {
                'market': m, 'slug': slug, 'side': side, 'token_id': token,
                'opp_token_id': dn_t if side == 'UP' else up_t,
                'best_ask': book['ask'], 'seconds_left': sec_left,
                'impulse': impulse,
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
    emit({'status': 'session_start', 'profile': args.profile,
          'execute': args.execute, 'stake_usd': cfg['stake_usd']})

    cap_reason = daily.check_caps(cfg['max_trades_per_day'], cfg['daily_max_loss_usd'])
    if cap_reason:
        report.update({'finished_at': ts_utc(), 'result': 'blocked_by_daily_caps',
                       'reason': cap_reason, 'daily_state': daily.state})
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    opened = None
    close_reason = None
    try:
        deadline = time.time() + cfg['entry_timeout_min'] * 60
        entry = find_entry(md, cfg, deadline, cfg['poll_sec'], cfg['kill_switch_errors'])
        if not entry:
            report.update({'finished_at': ts_utc(), 'result': 'no_entry_timeout'})
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return

        post = ex.marketable_buy(entry['token_id'], entry['best_ask'], cfg['stake_usd'])
        if not is_matched(post):
            report.update({'finished_at': ts_utc(), 'result': 'open_failed',
                           'open_post': post})
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return

        shares, cost = post_amounts(post, BUY)
        if not ex.execute:
            # simulate the fill at the marketable price
            px = round(clamp(entry['best_ask'] + BUY_SLIPPAGE, 0.01, 0.99), 2)
            shares = round(cfg['stake_usd'] / px, 2)
            cost = round(shares * entry['best_ask'], 6)
        entry_price = (cost / shares) if shares else entry['best_ask']
        opened = {
            'opened_at': ts_utc(),
            'market_slug': entry['slug'],
            'market_end_ts': entry['market']['_end_ts'],
            'side': entry['side'],
            'token_id': entry['token_id'],
            'opp_token_id': entry['opp_token_id'],
            'entry_price': round(entry_price, 6),
            'shares': shares,
            'cost_usdc': cost,
            'open_order_id': post.get('orderID'),
            'open_tx': (post.get('transactionsHashes') or [None])[0],
            'btc_impulse': entry['impulse'],
        }
        report['opened'] = opened
        emit({'status': 'opened', 'side': opened['side'], 'slug': opened['market_slug'],
              'entry_price': opened['entry_price'], 'shares': shares, 'cost_usdc': cost})

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

    # From here on, finish the close even if another stop signal arrives.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)

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
