\
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_profit_monitor.py (env版本)

变化点：
- 默认从 .env 读取两个地址：
    OP_WALLET_ADDRESS=0x...
    PM_WALLET_ADDRESS=0x...
  不再强制要求你在命令行手动输入钱包。
- 仍支持命令行覆盖：
    --op-wallet / --pm-wallet
- 保留兼容参数 --wallet（把同一个地址同时当作 OP/PM）

功能：
  Opinion 持仓 + Polymarket 持仓，根据 market_token_pairs.json 的 tokenId 映射配对反向持仓。
  若 best bid 相加 > threshold（默认 1.0），则发 Telegram【获利提醒】。

测试（只跑一轮 + 不发电报）：
  python run_profit_monitor.py --once --dry-run
"""

import os
import json
import time
import argparse
import random
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

# ---------- endpoints ----------
OPINION_BASE_URL = "https://openapi.opinion.trade/openapi"
OPINION_POSITIONS_ENDPOINT = f"{OPINION_BASE_URL}/positions/user"
OPINION_ORDERBOOK_ENDPOINT = f"{OPINION_BASE_URL}/token/orderbook"

POLY_DATA_POSITIONS_ENDPOINT = "https://data-api.polymarket.com/positions"
POLY_CLOB_BASE_URL = "https://clob.polymarket.com"
POLY_BOOKS_BATCH_ENDPOINT = f"{POLY_CLOB_BASE_URL}/books"  # POST batch

MARKET_JSON_DEFAULT = os.path.join(os.path.dirname(__file__), "market_token_pairs.json")

# ---------- telegram ----------
TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TG_THREAD_ID = os.getenv("TELEGRAM_MESSAGE_THREAD_ID")  # optional

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = (6, 25)


# ===================== helpers =====================
def ffloat(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _mk_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=80, pool_maxsize=80)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


_HTTP = _mk_session()
_TG = _mk_session()


def request_json(method: str, url: str, *, headers=None, params=None, json_body=None, timeout=HTTP_TIMEOUT) -> Any:
    r = _HTTP.request(method, url, headers=headers, params=params, json=json_body, timeout=timeout)
    try:
        return r.json()
    except Exception:
        return {"_non_json": True, "status": r.status_code, "text": (r.text or "")[:800]}


def tg_send(text: str, dry_run: bool):
    if dry_run or (not TG_BOT_TOKEN) or (not TG_CHAT_ID):
        print("\n" + text + "\n")
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True}
    if TG_THREAD_ID:
        payload["message_thread_id"] = int(TG_THREAD_ID)

    try:
        r = _TG.post(url, json=payload, timeout=(6, 18))
        if r.status_code != 200:
            print("[WARN] Telegram send failed:", r.status_code, (r.text or "")[:300])
    except Exception as e:
        print("[WARN] Telegram exception:", e)


def parse_best_bid(book_obj: Any) -> Tuple[Optional[float], Optional[float]]:
    """
    返回 (best_bid, bid_size)
    支持：
      - Polymarket /books: {"token_id": "...", "bids":[{"price":"0.29","size":"25.18"}], ...}
      - Opinion orderbook: {"result":{"data":{"bids":[...],"asks":[...]}}} 或 {"result":{"bids":[...],...}}
    """
    if not isinstance(book_obj, dict):
        return None, None

    # Opinion wrapper
    if isinstance(book_obj.get("result"), dict):
        inner = book_obj["result"].get("data") or book_obj["result"]
        if isinstance(inner, dict):
            book_obj = inner

    bids = book_obj.get("bids") or []
    if not isinstance(bids, list) or not bids:
        return None, None

    bids_sorted = sorted(bids, key=lambda x: ffloat(x.get("price")) or -1e18, reverse=True)
    top = bids_sorted[0]
    return ffloat(top.get("price")), ffloat(top.get("size"))


def make_opinion_url(parent_topic_id: Optional[int], mtype: str) -> str:
    if not parent_topic_id:
        return "https://app.opinion.trade/"
    if mtype == "categorical":
        return f"https://app.opinion.trade/detail?topicId={int(parent_topic_id)}&type=multi"
    return f"https://app.opinion.trade/detail?topicId={int(parent_topic_id)}"


def make_polymarket_event_url(event_slug: Optional[str]) -> str:
    if not event_slug:
        return "https://polymarket.com/"
    return f"https://polymarket.com/event/{event_slug}"


def load_opinion_keys() -> List[str]:
    raw = os.getenv("OPINION_API_KEYS", "").strip()
    one = os.getenv("OPINION_API_KEY", "").strip()
    keys: List[str] = []
    if raw:
        keys = [k for k in re.split(r"[,\s]+", raw) if k]
    if (not keys) and one:
        keys = [one]
    return keys


# ===================== market config -> legs =====================
def build_legs(market_json: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    legs: List[Dict[str, Any]] = []
    for item in market_json:
        name = item.get("name") or item.get("title") or "UNKNOWN_MARKET"
        mtype = item.get("type") or ("categorical" if item.get("pairs") else "binary")

        if item.get("pairs"):
            parent_id = item.get("opinion_market_id") or item.get("opinion_parent_id") or item.get("topicId")
            pm_event_slug = item.get("polymarket_event_slug") or item.get("pm_event_slug")
            for p in item.get("pairs") or []:
                cand = p.get("candidate") or "UNKNOWN_CANDIDATE"
                op = p.get("opinion") or {}
                pm = p.get("polymarket") or {}
                op_yes = str(op.get("yes_token_id") or "")
                op_no = str(op.get("no_token_id") or "")
                pm_yes = str(pm.get("yes_token_id") or "")
                pm_no = str(pm.get("no_token_id") or "")
                if not (op_yes and op_no and pm_yes and pm_no):
                    continue
                pm_end = pm.get("endDate") or item.get("polymarket_event_endDate") or item.get("polymarket_event_end_date")
                legs.append({
                    "type": "categorical",
                    "name": name,
                    "candidate": cand,
                    "op_yes": op_yes,
                    "op_no": op_no,
                    "pm_yes": pm_yes,
                    "pm_no": pm_no,
                    "opinion_parent_id": int(parent_id) if parent_id else None,
                    "pm_event_slug": pm_event_slug,
                    "pm_endDate": pm_end,
                })
            continue

        op = item.get("opinion") or {}
        pm = item.get("polymarket") or {}
        op_yes = str(op.get("yes_token_id") or "")
        op_no = str(op.get("no_token_id") or "")
        pm_yes = str(pm.get("yes_token_id") or "")
        pm_no = str(pm.get("no_token_id") or "")

        if not (pm_yes and pm_no):
            clob = pm.get("clobTokenIds") or pm.get("clob_token_ids") or []
            outcomes = pm.get("outcomes") or pm.get("outcome") or []
            if isinstance(clob, list) and len(clob) >= 2:
                if isinstance(outcomes, list) and len(outcomes) >= 2:
                    u = [str(x).upper() for x in outcomes]
                    try:
                        yes_i = u.index("YES")
                        no_i = u.index("NO")
                        pm_yes = str(clob[yes_i])
                        pm_no = str(clob[no_i])
                    except Exception:
                        pm_yes = str(clob[0])
                        pm_no = str(clob[1])
                else:
                    pm_yes = str(clob[0])
                    pm_no = str(clob[1])

        if not (op_yes and op_no and pm_yes and pm_no):
            continue

        parent_id = item.get("opinion_market_id") or item.get("opinion_parent_id") or item.get("topicId")
        pm_event_slug = item.get("polymarket_event_slug") or item.get("pm_event_slug") or pm.get("eventSlug") or pm.get("event_slug")
        pm_end = pm.get("endDate") or item.get("polymarket_event_endDate") or item.get("polymarket_event_end_date")
        legs.append({
            "type": "binary",
            "name": name,
            "candidate": "YES/NO",
            "op_yes": op_yes,
            "op_no": op_no,
            "pm_yes": pm_yes,
            "pm_no": pm_no,
            "opinion_parent_id": int(parent_id) if parent_id else None,
            "pm_event_slug": pm_event_slug,
            "pm_endDate": pm_end,
        })

    return legs


# ===================== positions fetch =====================
def opinion_fetch_positions(wallet: str, api_key: str, min_shares: float) -> Dict[str, float]:
    out: Dict[str, float] = {}
    page = 1
    limit = 50
    headers = {"apikey": api_key, "Accept": "application/json", "User-Agent": USER_AGENT}

    while True:
        url = f"{OPINION_POSITIONS_ENDPOINT}/{wallet}"
        obj = request_json("GET", url, headers=headers, params={"page": page, "limit": limit}, timeout=HTTP_TIMEOUT)

        items = None
        if isinstance(obj, dict):
            if isinstance(obj.get("result"), dict):
                r = obj["result"]
                if isinstance(r.get("data"), dict) and isinstance(r["data"].get("list"), list):
                    items = r["data"]["list"]
                elif isinstance(r.get("list"), list):
                    items = r["list"]
                elif isinstance(r.get("data"), list):
                    items = r["data"]
            if items is None and isinstance(obj.get("data"), dict) and isinstance(obj["data"].get("list"), list):
                items = obj["data"]["list"]
            if items is None and isinstance(obj.get("data"), list):
                items = obj["data"]
            if items is None and isinstance(obj.get("list"), list):
                items = obj["list"]
        elif isinstance(obj, list):
            items = obj

        if not items:
            break

        for it in items:
            if not isinstance(it, dict):
                continue
            token_id = it.get("tokenId") or it.get("token_id")
            shares = it.get("sharesOwned") or it.get("shares_owned") or it.get("shares") or it.get("size")
            if token_id is None:
                continue
            s = ffloat(shares)
            if s is None or s < float(min_shares):
                continue
            out[str(token_id)] = s

        if not items:
            break
        page += 1
        continue


    return out


def polymarket_fetch_positions(user: str, min_shares: float) -> Dict[str, float]:
    out: Dict[str, float] = {}
    offset = 0
    limit = 500

    while True:
        params = {
            "user": user,
            "limit": limit,
            "offset": offset,
            "sizeThreshold": min_shares,
            "sortBy": "TOKENS",
            "sortDirection": "DESC",
        }
        arr = request_json("GET", POLY_DATA_POSITIONS_ENDPOINT, params=params, timeout=HTTP_TIMEOUT)
        if not isinstance(arr, list) or not arr:
            break

        for it in arr:
            if not isinstance(it, dict):
                continue
            token_id = it.get("asset")
            size = it.get("size")
            s = ffloat(size)
            if token_id is None or s is None or s < float(min_shares):
                continue
            out[str(token_id)] = s

        if len(arr) < limit:
            break
        offset += limit
        if offset > 50000:
            break

    return out


# ===================== orderbooks =====================
def opinion_fetch_orderbook_bid(token_id: str, api_key: str) -> Tuple[Optional[float], Optional[float]]:
    headers = {"apikey": api_key, "Accept": "application/json", "User-Agent": USER_AGENT}
    obj = request_json("GET", OPINION_ORDERBOOK_ENDPOINT, headers=headers, params={"token_id": token_id}, timeout=HTTP_TIMEOUT)
    if isinstance(obj, dict):
        errno = obj.get("errno")
        if errno is not None and errno != 0:
            return None, None
    return parse_best_bid(obj)


def polymarket_fetch_books_batch(token_ids: List[str], chunk_size: int = 200) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not token_ids:
        return out

    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}

    for i in range(0, len(token_ids), chunk_size):
        chunk = token_ids[i:i + chunk_size]
        body = [{"token_id": tid} for tid in chunk]
        arr = request_json("POST", POLY_BOOKS_BATCH_ENDPOINT, headers=headers, json_body=body, timeout=(8, 28))

        if isinstance(arr, list):
            for obj in arr:
                if isinstance(obj, dict):
                    tid = str(obj.get("token_id") or obj.get("asset_id") or "")
                    if tid:
                        out[tid] = obj

        for tid in chunk:
            out.setdefault(tid, {"bids": [], "asks": []})

    return out


# ===================== profit alert =====================
def format_profit_alert(
    leg: Dict[str, Any],
    direction: str,
    sum_bid: float,
    pm_bid: float,
    pm_bidsz: Optional[float],
    op_bid: float,
    op_bidsz: Optional[float],
) -> str:
    op_url = make_opinion_url(leg.get("opinion_parent_id"), leg.get("type", "binary"))
    pm_url = make_polymarket_event_url(leg.get("pm_event_slug"))

    lines: List[str] = []
    lines.append("【获利提醒】【获利提醒】【获利提醒】")
    lines.append(f"{leg['name']} | {leg['candidate']}")
    lines.append(f"方向: {direction}")
    lines.append(f"Opinion: {op_url}")
    lines.append(f"Polymarket: {pm_url}")
    lines.append(f"PM 价格(bid): {pm_bid:.4f}, size={pm_bidsz if pm_bidsz is not None else 'NA'}")
    lines.append(f"OP 价格(bid): {op_bid:.4f}, size={op_bidsz if op_bidsz is not None else 'NA'}")
    lines.append(f"合计成本: {sum_bid:.2f}")
    return "\n".join(lines)


def run_once(
    op_wallet: str,
    pm_wallet: str,
    market_json_path: str,
    min_shares: float,
    min_bid_size: float,
    threshold: float,
    cooldown_sec: int,
    dry_run: bool,
    state: Dict[str, float],
):
    with open(market_json_path, "r", encoding="utf-8") as f:
        market_json = json.load(f)
    legs = build_legs(market_json)
    if not legs:
        print("[WARN] market_token_pairs.json 未解析出任何 legs")
        return

    keys = load_opinion_keys()
    if not keys:
        raise RuntimeError("未配置 OPINION_API_KEYS 或 OPINION_API_KEY")
    api_key = random.choice(keys)

    op_pos = opinion_fetch_positions(op_wallet, api_key, min_shares=min_shares)
    pm_pos = polymarket_fetch_positions(pm_wallet, min_shares=min_shares)

    print(f"[INFO] wallets: OP={op_wallet} PM={pm_wallet}")
    print(f"[INFO] positions: OP={len(op_pos)} PM={len(pm_pos)} (min_shares={min_shares})")

    matched: List[Tuple[Dict[str, Any], str, str, str]] = []
    for leg in legs:
        pm_yes = str(leg["pm_yes"])
        pm_no = str(leg["pm_no"])
        op_yes = str(leg["op_yes"])
        op_no = str(leg["op_no"])

        if pm_yes in pm_pos and op_no in op_pos:
            matched.append((leg, "卖 PM(YES) + 卖 OP(NO)", pm_yes, op_no))
        if pm_no in pm_pos and op_yes in op_pos:
            matched.append((leg, "卖 PM(NO) + 卖 OP(YES)", pm_no, op_yes))

    if not matched:
        print("[INFO] 没有找到可配对的跨平台反向持仓（PM YES+OP NO 或 PM NO+OP YES）")
        return

    print(f"[INFO] matched pairs: {len(matched)}")

    pm_token_ids = sorted({m[2] for m in matched})
    pm_books = polymarket_fetch_books_batch(pm_token_ids)

    alerts = 0
    now = time.time()

    for (leg, direction, pm_tid, op_tid) in matched:
        pm_book = pm_books.get(pm_tid) or {"bids": [], "asks": []}
        pm_bb, pm_bbs = parse_best_bid(pm_book)
        op_bb, op_bbs = opinion_fetch_orderbook_bid(op_tid, api_key)

        if pm_bb is None or op_bb is None:
            continue

        sum_bid = float(pm_bb) + float(op_bb)

        #只按盘口 size 过滤
        pm_sz = float(pm_bbs or 0.0)
        op_sz = float(op_bbs or 0.0)
        if min(pm_sz, op_sz) < float(min_bid_size):
            # 建议加一行 debug，方便你确认过滤在生效
            print(f"[SKIP] size too small: pm={pm_sz:.4f} op={op_sz:.4f} (< {min_bid_size})")
            continue

        key = f"{leg.get('pm_event_slug','')}/{leg.get('candidate','')}/{direction}"
        last = state.get(key, 0.0)
        ok_cooldown = (now - last) >= float(cooldown_sec)

        print(f"[CHECK] {leg['name']} | {leg['candidate']} | {direction} | sum_bid={sum_bid:.4f} (pm={pm_bb:.4f}, op={op_bb:.4f})")

        if sum_bid > float(threshold) and ok_cooldown:
            msg = format_profit_alert(
                leg=leg,
                direction=direction,
                sum_bid=sum_bid,
                pm_bid=float(pm_bb),
                pm_bidsz=pm_bbs,
                op_bid=float(op_bb),
                op_bidsz=op_bbs,
            )
            alerts += 1
            state[key] = now
            tg_send(msg, dry_run=dry_run)

    print(f"[INFO] alerts sent (or would send): {alerts}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=MARKET_JSON_DEFAULT, help="market_token_pairs.json 路径")
    ap.add_argument("--interval", type=int, default=120, help="扫描间隔秒数（默认 120=2分钟）")
    ap.add_argument("--min-shares", type=float, default=10.0, help="过滤持仓（sharesOwned/size）小于该值的仓位")
    ap.add_argument("--min-bid-size", type=float, default=10.0, help="过滤 best bid 的挂单量（PM/OP 任一边 size 小于该值就不提醒）")
    ap.add_argument("--threshold", type=float, default=1.0, help="当 best bid 相加大于该值则提醒（默认 1.0）")
    ap.add_argument("--cooldown", type=int, default=1800, help="同一条提醒最短重复间隔秒数（默认 1800=30分钟）")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出（测试用）")
    ap.add_argument("--dry-run", action="store_true", help="不发电报，只打印（测试用）")

    ap.add_argument("--wallet", default=os.getenv("WALLET_ADDRESS", "").strip(),
                    help="(兼容旧用法) 同时作为 OP/PM 钱包地址；优先级低于 --op-wallet/--pm-wallet")
    ap.add_argument("--op-wallet", default=os.getenv("OP_WALLET_ADDRESS", "").strip(),
                    help="Opinion 钱包地址（默认读取 .env: OP_WALLET_ADDRESS）")
    ap.add_argument("--pm-wallet", default=os.getenv("PM_WALLET_ADDRESS", os.getenv("POLYMARKET_USER", "")).strip(),
                    help="Polymarket user 地址（默认读取 .env: PM_WALLET_ADDRESS）")

    args = ap.parse_args()

    if not os.path.exists(args.json):
        raise SystemExit(f"找不到 {args.json}")

    op_wallet = (args.op_wallet or "").strip() or (args.wallet or "").strip()
    pm_wallet = (args.pm_wallet or "").strip() or (args.wallet or "").strip()

    if not op_wallet:
        raise SystemExit("缺少 Opinion 钱包地址：请在 .env 设置 OP_WALLET_ADDRESS 或传 --op-wallet")
    if not pm_wallet:
        raise SystemExit("缺少 Polymarket 钱包地址：请在 .env 设置 PM_WALLET_ADDRESS 或传 --pm-wallet")

    state: Dict[str, float] = {}

    while True:
        try:
            run_once(
                op_wallet=op_wallet,
                pm_wallet=pm_wallet,
                market_json_path=args.json,
                min_shares=float(args.min_shares),
                min_bid_size=float(args.min_bid_size),
                threshold=float(args.threshold),
                cooldown_sec=int(args.cooldown),
                dry_run=bool(args.dry_run),
                state=state,
            )
        except Exception as e:
            print("[ERROR]", e)

        if args.once:
            break
        time.sleep(max(30, int(args.interval)))


if __name__ == "__main__":
    main()
