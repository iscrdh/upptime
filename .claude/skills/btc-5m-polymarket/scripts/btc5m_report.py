#!/usr/bin/env python3
"""Summarize recent btc5m runs (PnL, results, close statuses) from runtime logs."""
import argparse
import glob
import json
import os
from collections import Counter
from pathlib import Path


def default_runtime_dir() -> str:
    return str(Path(__file__).resolve().parents[1] / "runtime")


def load_tail_json(path: str):
    """Parse the final indented JSON report printed at the end of a run log."""
    try:
        txt = open(path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    i = txt.rfind("\n{\n")
    if i == -1 and txt.startswith("{\n"):
        i = 0
    if i == -1:
        return None
    blob = txt[i + 1:] if txt[i:i + 1] == "\n" else txt[i:]
    try:
        return json.loads(blob)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runtime-dir", default=default_runtime_dir())
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    files = sorted(
        glob.glob(os.path.join(args.runtime_dir, "btc5m_*.log")),
        key=os.path.getmtime, reverse=True,
    )[: args.limit]

    rows = []
    total_pnl = 0.0
    pnl_count = 0
    close_status = Counter()
    results = Counter()

    for f in files:
        obj = load_tail_json(f)
        if not obj:
            continue
        r = obj.get("result")
        results[r] += 1
        op = obj.get("opened") or {}
        cl = obj.get("closed") or {}
        pnl = obj.get("realized_cashflow_pnl_usdc")
        if isinstance(pnl, (int, float)):
            total_pnl += float(pnl)
            pnl_count += 1
        close_status[str(cl.get("close_status") or "none")] += 1
        rows.append(
            {
                "file": os.path.basename(f),
                "result": r,
                "execute": (obj.get("params") or {}).get("execute"),
                "side": op.get("side"),
                "market": op.get("market_slug"),
                "close_reason": cl.get("close_reason"),
                "close_status": cl.get("close_status"),
                "open_tx": op.get("open_tx"),
                "close_tx": cl.get("close_tx"),
                "pnl": pnl,
            }
        )

    out = {
        "logs_scanned": len(files),
        "runs_parsed": len(rows),
        "results": dict(results),
        "close_status": dict(close_status),
        "realized_pnl_sum_usdc": round(total_pnl, 6) if pnl_count else None,
        "realized_pnl_count": pnl_count,
        "runs": rows,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
