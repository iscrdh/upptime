#!/usr/bin/env python3
"""Evaluate collected data: is the value model calibrated, and would it pay?

Reads runtime/dataset.csv (from scripts/btc5m_collect.py) and reports:
- Brier score + reliability buckets (model probability vs. actual outcomes)
- Simulated PnL of the value rule (taker fill at ask, hold to resolution)
  across a grid of edge thresholds and entry checkpoints

If avg PnL per trade is not clearly positive across nearby thresholds and
checkpoints (not just one lucky cell), assume there is no edge.
"""

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


def load(path: Path):
    samples, outcomes = [], {}
    with path.open(newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row['kind'] == 'outcome':
                outcomes[row['slug']] = row['outcome']
            elif row['kind'] == 'sample':
                samples.append(row)
    return samples, outcomes


def f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dataset', default=None)
    ap.add_argument('--fee-buffer', type=float, default=0.01)
    ap.add_argument('--max-entry-price', type=float, default=0.95)
    ap.add_argument('--edges', default='0.02,0.03,0.04,0.05,0.07,0.10')
    args = ap.parse_args()

    path = Path(args.dataset) if args.dataset else \
        Path(__file__).resolve().parents[1] / 'runtime' / 'dataset.csv'
    samples, outcomes = load(path)
    resolved = {s for s, o in outcomes.items() if o in ('UP', 'DOWN')}
    rows = [s for s in samples if s['slug'] in resolved]

    # --- calibration ---
    brier_sum, n = 0.0, 0
    buckets = defaultdict(lambda: [0, 0.0, 0.0])  # bucket -> [count, sum_p, sum_won]
    for s in rows:
        p = f(s['p_up'])
        if p is None:
            continue
        y = 1.0 if outcomes[s['slug']] == 'UP' else 0.0
        brier_sum += (p - y) ** 2
        n += 1
        b = min(9, int(p * 10))
        buckets[b][0] += 1
        buckets[b][1] += p
        buckets[b][2] += y
    calibration = {
        f'{b/10:.1f}-{(b+1)/10:.1f}': {
            'n': c[0],
            'model_avg': round(c[1] / c[0], 3),
            'actual_up_rate': round(c[2] / c[0], 3),
        }
        for b, c in sorted(buckets.items()) if c[0] > 0
    }

    # --- value-rule simulation: one trade max per (slot, checkpoint) ---
    checkpoints = sorted({round(f(s['sec_left'], 0) / 30) * 30 for s in rows})
    grid = {}
    for edge_min in (float(x) for x in args.edges.split(',')):
        for cp in checkpoints:
            pnls = []
            for s in rows:
                if round(f(s['sec_left'], 0) / 30) * 30 != cp:
                    continue
                p_up = f(s['p_up'])
                if p_up is None:
                    continue
                won_up = outcomes[s['slug']] == 'UP'
                best = None
                for side, ask, fair in (('UP', f(s['up_ask']), p_up),
                                        ('DOWN', f(s['dn_ask']), 1 - p_up)):
                    if ask is None or ask > args.max_entry_price:
                        continue
                    edge = fair - ask - args.fee_buffer
                    if edge >= edge_min and (best is None or edge > best[2]):
                        best = (side, ask, edge)
                if not best:
                    continue
                side, ask, _ = best
                won = won_up if side == 'UP' else not won_up
                pnls.append((1.0 - ask) if won else -ask)
            if pnls:
                grid[f'edge>={edge_min:.2f} @ ~{int(cp)}s'] = {
                    'trades': len(pnls),
                    'win_rate': round(sum(1 for x in pnls if x > 0) / len(pnls), 3),
                    'avg_pnl_per_$1': round(sum(pnls) / len(pnls), 4),
                    'total_pnl_per_$1_stake': round(sum(pnls), 3),
                }

    print(json.dumps({
        'dataset': str(path),
        'slots_resolved': len(resolved),
        'samples_used': n,
        'brier_score': round(brier_sum / n, 4) if n else None,
        'brier_baseline_coinflip': 0.25,
        'calibration_by_bucket': calibration,
        'value_rule_grid': grid,
        'read_me': 'edge exists only if avg_pnl_per_$1 is consistently > 0 across '
                   'neighboring cells with trades >= 30; one positive cell is noise',
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
