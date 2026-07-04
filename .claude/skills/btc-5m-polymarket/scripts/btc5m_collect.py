#!/usr/bin/env python3
"""Collect calibration data for the BTC 5m value model. Places NO orders.

Every 5m slot: sample model inputs/outputs and both order books at fixed
checkpoints (seconds before close), then record the resolved outcome.
Appends CSV rows to runtime/dataset.csv for scripts/btc5m_eval.py.

Run this for a few days before trusting the strategy with money:
  .venv/bin/python scripts/btc5m_collect.py --hours 24
"""

import argparse
import csv
import datetime as dt
import time
from pathlib import Path

from btc5m_trade import (
    DEFAULT_CONFIG, MarketData, bucket_5m, btc_impulse_usd, emit,
    estimate_vol_1m_usd, load_config, model_prob_up, ts_utc, wait_for_resolution,
)

FIELDS = [
    'ts', 'slug', 'kind', 'sec_left', 'btc_move_usd', 'vol_1m_usd', 'p_up',
    'up_bid', 'up_ask', 'dn_bid', 'dn_ask', 'outcome',
]


def append_row(path: Path, row: dict) -> None:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, '') for k in FIELDS})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--hours', type=float, default=24.0)
    ap.add_argument('--checkpoints', default='180,150,120,90,60',
                    help='comma-separated seconds-before-close sample points')
    ap.add_argument('--out', default=None, help='CSV path (default runtime/dataset.csv)')
    ap.add_argument('--config', default=str(DEFAULT_CONFIG))
    ap.add_argument('--profile', default='conservative')
    args = ap.parse_args()

    cfg = load_config(Path(args.config), args.profile)
    md = MarketData(cfg['gamma_base'], cfg['clob_base'])
    out = Path(args.out) if args.out else Path(__file__).resolve().parents[1] / 'runtime' / 'dataset.csv'
    checkpoints = sorted({int(x) for x in args.checkpoints.split(',')}, reverse=True)
    stop_at = time.time() + args.hours * 3600
    vol_cache = {'t': 0.0, 'v': None}

    emit({'status': 'collector_start', 'out': str(out), 'checkpoints': checkpoints,
          'hours': args.hours})

    while time.time() < stop_at:
        bucket = bucket_5m(int(time.time()))
        end_ts = bucket + 300
        slug = f'btc-updown-5m-{bucket}'
        sampled = 0
        for target in checkpoints:
            wait = (end_ts - target) - time.time()
            if wait < -5:
                continue  # checkpoint already passed for this slot
            if wait > 0:
                time.sleep(wait)
            try:
                m = md.resolve_current_5m_market(cfg['require_end_in_future_sec_min'])
                if not m or m['_event_slug'] != slug:
                    emit({'status': 'sample_no_market', 'slug': slug, 'target': target})
                    continue
                up_t, dn_t = md.side_tokens(m)
                up = md.book_top(up_t)
                dn = md.book_top(dn_t)
                impulse = btc_impulse_usd(bucket, md.http)
                if time.time() - vol_cache['t'] > 60:
                    vol_cache['v'] = estimate_vol_1m_usd(md.http, cfg['vol_lookback_min'])
                    vol_cache['t'] = time.time()
                vol = vol_cache['v']
                if impulse is None or vol is None:
                    emit({'status': 'sample_feed_unavailable', 'slug': slug, 'target': target})
                    continue
                sec_left = max(0.0, m['_end_ts'] - time.time())
                p_up = model_prob_up(impulse['move'], vol, sec_left)
                append_row(out, {
                    'ts': ts_utc(), 'slug': slug, 'kind': 'sample',
                    'sec_left': round(sec_left, 1),
                    'btc_move_usd': round(impulse['move'], 2),
                    'vol_1m_usd': round(vol, 2), 'p_up': round(p_up, 4),
                    'up_bid': up['bid'], 'up_ask': up['ask'],
                    'dn_bid': dn['bid'], 'dn_ask': dn['ask'],
                })
                sampled += 1
            except Exception as e:
                emit({'status': 'sample_error', 'slug': slug, 'error': str(e)})

        if sampled:
            try:
                outcome = wait_for_resolution(
                    md, cfg, {'market_slug': slug, 'market_end_ts': end_ts})
                append_row(out, {'ts': ts_utc(), 'slug': slug, 'kind': 'outcome',
                                 'outcome': outcome or 'UNRESOLVED'})
                emit({'status': 'slot_done', 'slug': slug, 'samples': sampled,
                      'outcome': outcome})
            except Exception as e:
                emit({'status': 'resolution_error', 'slug': slug, 'error': str(e)})
        else:
            # nothing sampled this slot; wait for the next one
            time.sleep(max(1.0, end_ts + 2 - time.time()))

    emit({'status': 'collector_done', 'out': str(out)})


if __name__ == '__main__':
    main()
