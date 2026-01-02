#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
对账 Opinion vs Polymarket 仓位：
- OP 的 NO 对应 PM 的 YES
- OP 的 YES 对应 PM 的 NO

输出 JSON：列出 abs(op_shares - pm_shares) > threshold 的所有对账项。
"""

from __future__ import annotations

import os
import json
import time
import argparse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# 直接复用你现有的、已经跑通的实现（避免重复造轮子）
from run_profit_monitor_env import (
    build_legs,
    load_opinion_keys,
    opinion_fetch_positions,
    polymarket_fetch_positions,
    make_opinion_url,
    make_polymarket_event_url,
)

load_dotenv()


def _get_wallet(primary_env: str, fallback_env: str) -> str:
    v = (os.getenv(primary_env) or "").strip()
    if v:
        return v
    v = (os.getenv(fallback_env) or "").strip()
    if v:
        return v
    raise SystemExit(f"Missing wallet address: set {primary_env} (or {fallback_env})")


def _load_market_json(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, list):
        raise SystemExit(f"market json must be a list, got {type(obj)}")
    return obj


def _fetch_op_positions_with_fallback(wallet: str, keys: List[str], min_shares_fetch: float) -> Dict[str, float]:
    last_err: Optional[BaseException] = None
    for k in keys:
        try:
            return opinion_fetch_positions(wallet, k, min_shares_fetch)
        except Exception as e:
            last_err = e
            continue
    raise SystemExit(f"Opinion positions fetch failed for all keys: {last_err}")


def _build_checks(legs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    for leg in legs:
        # OP NO <-> PM YES
        checks.append(
            {
                "map": "OP_NO_to_PM_YES",
                "market": leg.get("name"),
                "candidate": leg.get("candidate"),
                "mtype": leg.get("type"),
                "op_token": leg.get("op_no"),
                "op_side": "NO",
                "pm_token": leg.get("pm_yes"),
                "pm_side": "YES",
                "opinion_parent_id": leg.get("opinion_parent_id"),
                "pm_event_slug": leg.get("pm_event_slug"),
                "pm_endDate": leg.get("pm_endDate"),
            }
        )
        # OP YES <-> PM NO
        checks.append(
            {
                "map": "OP_YES_to_PM_NO",
                "market": leg.get("name"),
                "candidate": leg.get("candidate"),
                "mtype": leg.get("type"),
                "op_token": leg.get("op_yes"),
                "op_side": "YES",
                "pm_token": leg.get("pm_no"),
                "pm_side": "NO",
                "opinion_parent_id": leg.get("opinion_parent_id"),
                "pm_event_slug": leg.get("pm_event_slug"),
                "pm_endDate": leg.get("pm_endDate"),
            }
        )
    return checks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-json", default=os.path.join(os.path.dirname(__file__), "market_token_pairs.json"))
    ap.add_argument("--op-wallet", default=(os.getenv("OP_WALLET_ADDRESS") or "").strip() or (os.getenv("WALLET_ADDRESS") or "").strip())
    ap.add_argument("--pm-wallet", default=(os.getenv("PM_WALLET_ADDRESS") or "").strip() or (os.getenv("WALLET_ADDRESS") or "").strip())
    ap.add_argument("--threshold", type=float, default=10.0, help="abs(op_shares - pm_shares) > threshold => report")
    ap.add_argument("--min-shares-fetch", type=float, default=0.0, help="fetch positions with this size filter (performance)")
    ap.add_argument("--out", default="position_diff.json")
    ap.add_argument("--include-unmapped", action="store_true", help="also include tokens held but not in market_token_pairs (>= threshold)")
    args = ap.parse_args()

    op_wallet = args.op_wallet or _get_wallet("OP_WALLET_ADDRESS", "WALLET_ADDRESS")
    pm_wallet = args.pm_wallet or _get_wallet("PM_WALLET_ADDRESS", "WALLET_ADDRESS")

    keys = load_opinion_keys()
    if not keys:
        raise SystemExit("Missing Opinion API key(s): set OPINION_API_KEYS or OPINION_API_KEY")

    market_json = _load_market_json(args.market_json)
    legs = build_legs(market_json)
    checks = _build_checks(legs)

    t0 = time.time()
    op_pos = _fetch_op_positions_with_fallback(op_wallet, keys, args.min_shares_fetch)
    pm_pos = polymarket_fetch_positions(pm_wallet, args.min_shares_fetch)
    t1 = time.time()

    mismatches: List[Dict[str, Any]] = []
    mapped_tokens = set()

    for c in checks:
        op_token = str(c["op_token"])
        pm_token = str(c["pm_token"])
        mapped_tokens.add(op_token)
        mapped_tokens.add(pm_token)

        op_shares = float(op_pos.get(op_token, 0.0))
        pm_shares = float(pm_pos.get(pm_token, 0.0))
        delta = op_shares - pm_shares
        abs_delta = abs(delta)

        if abs_delta > args.threshold:
            mismatches.append(
                {
                    **c,
                    "op_shares": op_shares,
                    "pm_shares": pm_shares,
                    "delta_op_minus_pm": delta,
                    "abs_delta": abs_delta,
                    "opinion_url": make_opinion_url(c.get("opinion_parent_id"), c.get("mtype") or ""),
                    "polymarket_url": make_polymarket_event_url(c.get("pm_event_slug")),
                }
            )

    report: Dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "op_wallet": op_wallet,
        "pm_wallet": pm_wallet,
        "threshold": args.threshold,
        "min_shares_fetch": args.min_shares_fetch,
        "fetch_seconds": round(t1 - t0, 3),
        "summary": {
            "legs": len(legs),
            "pairs_checked": len(checks),
            "mismatches": len(mismatches),
            "op_positions": len(op_pos),
            "pm_positions": len(pm_pos),
        },
        "mismatches": mismatches,
    }

    if args.include_unmapped:
        op_unmapped = {tid: s for tid, s in op_pos.items() if tid not in mapped_tokens and float(s) >= args.threshold}
        pm_unmapped = {tid: s for tid, s in pm_pos.items() if tid not in mapped_tokens and float(s) >= args.threshold}
        report["unmapped_positions_ge_threshold"] = {"opinion": op_unmapped, "polymarket": pm_unmapped}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"[OK] wrote {args.out} | mismatches={len(mismatches)} | fetched in {report['fetch_seconds']}s")


if __name__ == "__main__":
    main()
