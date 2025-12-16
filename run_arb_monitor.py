import os
import json
import time
import threading
import argparse
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

#V1.5 优化版本
#优化Opinion 为啥 22s：你每一轮都在“重建线程池 + 重建连接 + 双重重试”
#Polymarket 为啥 8s：你在 /books 后面“补齐 missing 单个 /book”，missing 很多时会爆炸

# ===================== 基础配置（你只改这里也可以） =====================
MARKET_JSON_DEFAULT = os.path.join(os.path.dirname(__file__), "market_token_pairs.json")

OPINION_BASE_URL = "https://openapi.opinion.trade/openapi"
OPINION_ORDERBOOK_ENDPOINT = f"{OPINION_BASE_URL}/token/orderbook"

POLY_CLOB_BASE_URL = "https://clob.polymarket.com"
POLY_BOOK_ENDPOINT = f"{POLY_CLOB_BASE_URL}/book"
POLY_BOOKS_BATCH_ENDPOINT = f"{POLY_CLOB_BASE_URL}/books"  # POST 批量

# Gamma（拿 event endDate）
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
GAMMA_EVENT_SLUG_ENDPOINT = f"{GAMMA_BASE_URL}/events/slug"

# 兼容：单 key（旧） + 多 key（新）
OPINION_API_KEY = os.getenv("OPINION_API_KEY")
OPINION_API_KEYS_RAW = os.getenv("OPINION_API_KEYS", "")

def get_opinion_keys() -> List[str]:
    # 支持逗号/空格分隔
    keys: List[str] = []
    if OPINION_API_KEYS_RAW.strip():
        keys = [k for k in re.split(r"[,\s]+", OPINION_API_KEYS_RAW.strip()) if k]

    # 兼容旧的单 key
    if not keys and OPINION_API_KEY and OPINION_API_KEY.strip():
        keys = [OPINION_API_KEY.strip()]

    return keys


TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TG_THREAD_ID = os.getenv("TELEGRAM_MESSAGE_THREAD_ID")  # 可选：论坛群 topic id

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 请求超时： (connect_timeout, read_timeout)
HTTP_TIMEOUT = (6, 20)

# ====== 过滤策略默认值（你要改规则就改这几个）======
MIN_DEPLOY_USD_DEFAULT = 20.0  # < $10 不提醒
MAX_DAYS_TO_EXPIRY_DEFAULT = 60        # 2) 距 endDate > 60 天不提醒
EVENT_META_TTL_SECONDS = 24 * 3600     # 事件元数据（endDate）每天刷新一次

# ===================== 小工具 =====================
def ffloat(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

def min2(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return min(a, b)

def iso_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # 兼容 "2026-01-01T00:00:00Z"
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2)
    except Exception:
        return None

def strip_slug_suffix(slug: str) -> Optional[str]:
    m = re.search(r"(.+)-\d+$", slug)
    return m.group(1) if m else None

class RateLimiter:
    """简单 QPS 限速器：多线程共享"""
    def __init__(self, qps: float):
        self.qps = max(0.1, float(qps))
        self.min_interval = 1.0 / self.qps
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
            self._next = time.monotonic() + self.min_interval

# ===================== Session（连接池 + 自动重试） =====================
_tls = threading.local()

def _build_session() -> requests.Session:
    s = requests.Session()

    adapter = HTTPAdapter(
        max_retries=0,          # ✅ 关闭 urllib3 自动重试（避免双重重试）
        pool_connections=256,
        pool_maxsize=256,
    )

    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def get_session(name: str) -> requests.Session:
    sess = getattr(_tls, name, None)
    if sess is None:
        sess = _build_session()
        setattr(_tls, name, sess)
    return sess

def request_json(
    method: str,
    url: str,
    *,
    session: requests.Session,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Any = None,
    limiter: Optional[RateLimiter] = None,
    timeout=HTTP_TIMEOUT,
    tries: int = 4,
) -> Any:
    """
    统一请求封装：
      - Session/连接池
      - 捕获 10053/超时/连接被断，指数退避重试
      - 对 429/5xx 也重试
    """
    last_err: Optional[Exception] = None

    for attempt in range(tries):
        if limiter:
            limiter.acquire()

        try:
            r = session.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=timeout,
            )

            if r.status_code == 200:
                return r.json()
            
            # ✅ Polymarket：没有订单簿是正常情况 -> 返回空簿，不重试
            if r.status_code == 404:
                txt = (r.text or "")[:500]
                if "No orderbook exists for the requested token id" in txt:
                    return {"bids": [], "asks": [], "_no_orderbook": True}
                # 其他 404 仍然是错误（比如 endpoint 不存在、token 参数错等）
                raise RuntimeError(f"HTTP 404: {txt}")

            if r.status_code in (429, 500, 502, 503, 504):
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        sleep_s = float(ra)
                    except Exception:
                        sleep_s = 0.6 * (2 ** attempt)
                else:
                    sleep_s = 0.6 * (2 ** attempt)

                sleep_s += random.random() * 0.2
                time.sleep(sleep_s)
                last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                continue

            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            last_err = e
            sleep_s = (0.5 * (2 ** attempt)) + random.random() * 0.2
            time.sleep(sleep_s)
            continue

        except Exception as e:
            last_err = e
            break

    raise RuntimeError(f"request failed after {tries} tries: {method} {url} last={last_err}")

# ===================== Telegram =====================
def tg_send(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[WARN] Telegram 未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，改为只打印：")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if TG_THREAD_ID:
        payload["message_thread_id"] = int(TG_THREAD_ID)

    try:
        s = get_session("tg")
        request_json(
            "POST",
            url,
            session=s,
            json_body=payload,
            limiter=None,          # 你有 cooldown，通常不需要再给 TG 限速
            timeout=(3, 10),       # TG 用短一点，别拖慢主循环
            tries=3,
        )
    except Exception as e:
        print("[WARN] Telegram exception:", e)


# ===================== 订单簿解析（统一：best bid/ask + size） =====================
def parse_best_bid_ask(book: Dict[str, Any]) -> Dict[str, Optional[float]]:
    # Opinion 可能在 result.data 下
    if "result" in book and isinstance(book["result"], dict):
        inner = book["result"].get("data") or book["result"]
        if isinstance(inner, dict):
            book = inner

    bids = book.get("bids") or []
    asks = book.get("asks") or []

    bids_sorted = sorted(bids, key=lambda x: ffloat(x.get("price")) or -1e18, reverse=True)
    asks_sorted = sorted(asks, key=lambda x: ffloat(x.get("price")) or 1e18, reverse=False)

    bb = ffloat(bids_sorted[0].get("price")) if bids_sorted else None
    bbs = ffloat(bids_sorted[0].get("size")) if bids_sorted else None
    ba = ffloat(asks_sorted[0].get("price")) if asks_sorted else None
    bas = ffloat(asks_sorted[0].get("size")) if asks_sorted else None

    return {
        "best_bid": bb,
        "best_bid_size": bbs,
        "best_ask": ba,
        "best_ask_size": bas,
    }

# ===================== Opinion 拉订单簿 =====================
def opinion_fetch_orderbook(token_id: str, api_key: str, limiter: RateLimiter) -> Dict[str, Any]:
    if not api_key:
        raise RuntimeError("Opinion api_key 为空（请检查 OPINION_API_KEYS / OPINION_API_KEY）")

    headers = {
        "apikey": api_key,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    s = get_session("opinion")
    data = request_json(
        "GET",
        OPINION_ORDERBOOK_ENDPOINT,
        session=s,
        headers=headers,
        params={"token_id": token_id},
        limiter=limiter,
        tries=4,
    )

    code = data.get("errno")
    if code is not None and code != 0:
        raise RuntimeError(f"Opinion orderbook errno={code}: {str(data)[:300]}")

    return data


# ===================== Polymarket 拉订单簿（批量优先） =====================
def polymarket_fetch_book_single(token_id: str, limiter: RateLimiter) -> Dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    s = get_session("poly")

    return request_json(
        "GET",
        POLY_BOOK_ENDPOINT,
        session=s,
        headers=headers,
        params={"token_id": token_id},
        limiter=limiter,
        tries=4,
    )

def polymarket_fetch_books_batch(
    token_ids: List[str],
    limiter: RateLimiter,
    chunk_size: int = 200,
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not token_ids:
        return out

    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
    s = get_session("poly")

    for i in range(0, len(token_ids), chunk_size):
        chunk = token_ids[i:i + chunk_size]
        body = [{"token_id": tid} for tid in chunk]

        try:
            arr = request_json(
                "POST",
                POLY_BOOKS_BATCH_ENDPOINT,
                session=s,
                headers=headers,
                json_body=body,
                limiter=limiter,
                tries=4,
                timeout=(8, 25),
            )
        except Exception as e:
            print(f"[WARN] Polymarket /books chunk failed, will fallback singles. err={e}")
            arr = None

        if isinstance(arr, list):
            for obj in arr:
                tid = str(obj.get("token_id") or obj.get("asset_id") or "")
                if tid:
                    out[tid] = obj

            if len(arr) == len(chunk):
                for tid, obj in zip(chunk, arr):
                    out.setdefault(tid, obj)

        missing = [tid for tid in chunk if tid not in out]
        if missing:
            # ✅ /books 没返回的 token，大概率就是没有 orderbook（404 也被你视为正常空簿）:contentReference[oaicite:13]{index=13}
            for tid in missing:
                out[tid] = {"bids": [], "asks": [], "_missing_from_books": True}

    return out

# ===================== Gamma：拿 event endDate（带缓存） =====================
_event_meta_lock = threading.Lock()
_event_meta_cache: Dict[str, Dict[str, Any]] = {}  # slug -> {"fetched_ts":..., "end_dt":..., "id":..., "title":...}

def gamma_get_event_meta(slug: str, limiter: RateLimiter) -> Optional[Dict[str, Any]]:
    if not slug:
        return None

    now = time.time()
    with _event_meta_lock:
        cached = _event_meta_cache.get(slug)
        if cached and (now - cached.get("fetched_ts", 0)) < EVENT_META_TTL_SECONDS:
            return cached

    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    s = get_session("gamma")

    def _fetch(slg: str) -> Optional[Dict[str, Any]]:
        url = f"{GAMMA_EVENT_SLUG_ENDPOINT}/{slg}"
        try:
            obj = request_json("GET", url, session=s, headers=headers, limiter=limiter, tries=3, timeout=(6, 18))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    ev = _fetch(slug)
    if ev is None:
        alt = strip_slug_suffix(slug)
        if alt:
            ev = _fetch(alt)

    if ev is None:
        # 拿不到 endDate：不做过滤（避免漏报）
        return None

    end_dt = iso_to_dt(ev.get("endDate"))
    meta = {
        "fetched_ts": now,
        "end_dt": end_dt,
        "id": str(ev.get("id") or ""),
        "title": ev.get("title") or "",
        "slug": slug,
    }

    with _event_meta_lock:
        _event_meta_cache[slug] = meta
    return meta

def event_is_within_days(slug: str, max_days: int, limiter: RateLimiter) -> bool:
    meta = gamma_get_event_meta(slug, limiter)
    if not meta:
        return True  # 拿不到就放行（避免漏掉）
    end_dt = meta.get("end_dt")
    if not isinstance(end_dt, datetime):
        return True
    now_utc = datetime.now(timezone.utc)
    delta_days = (end_dt - now_utc).total_seconds() / 86400.0
    # 已结束/马上结束：仍允许（你要严格也可以改成 delta_days>0 才放行）
    return delta_days <= float(max_days)

# ===================== URL 构造（电报里用） =====================

def leg_is_within_days(leg: Dict[str, Any], max_days: int, gamma_limiter: Optional[RateLimiter] = None) -> bool:
    """优先用 market_token_pairs.json 里每个 Polymarket 子市场的 endDate 来过滤。
    规则：endDate 距离现在 > max_days => 不参与监控/不提醒；endDate 已过期 => 不参与。
    如果 leg 没有 endDate（老 JSON），再 fallback 用 event slug 去 Gamma 查 endDate。
    """
    end_str = (
        leg.get("pm_endDate")
        or leg.get("pm_end_date")
        or leg.get("pm_enddate")
        or None
    )

    end_dt = iso_to_dt(end_str) if end_str else None

    # fallback（老 schema 没 endDate）
    if end_dt is None:
        slug = leg.get("pm_event_slug")
        if slug:
            meta = gamma_get_event_meta(slug, gamma_limiter)  # ✅补上 limiter
            if meta and isinstance(meta.get("end_dt"), datetime):
                end_dt = meta["end_dt"]  # ✅直接拿 datetime，不要再 iso_to_dt


    if end_dt is None:
        # 实在拿不到，就别过滤（避免误杀）
        return True

    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)

    now_utc = datetime.now(timezone.utc)
    delta = end_dt - now_utc

    # 已过期：不监控
    if delta.total_seconds() < 0:
        return False

    return delta <= timedelta(days=max_days)
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

# ===================== 从 market_token_pairs.json 构造“监控腿” =====================
def build_legs(market_json: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    legs: List[Dict[str, Any]] = []

    for item in market_json:
        mtype = item.get("type")
        name = item.get("name") or "UNKNOWN"

        if mtype == "binary":
            op = item.get("opinion") or {}
            pm = item.get("polymarket") or {}

            op_yes = str(op.get("yes_token_id") or "")
            op_no = str(op.get("no_token_id") or "")
            if not op_yes or not op_no:
                continue

            outcomes = pm.get("outcomes") or []
            clob = pm.get("clob_token_ids") or pm.get("clobTokenIds") or []
            if len(clob) != 2:
                continue

            pm_yes_idx, pm_no_idx = 0, 1
            if len(outcomes) == 2:
                o0 = str(outcomes[0]).lower()
                o1 = str(outcomes[1]).lower()
                if o0 == "no" and o1 == "yes":
                    pm_yes_idx, pm_no_idx = 1, 0

            pm_slug = str(pm.get("slug") or "")
            opinion_parent = op.get("market_id")

            legs.append(
                {
                    "type": "binary",
                    "name": name,
                    "candidate": f"{outcomes[0]}/{outcomes[1]}" if len(outcomes) == 2 else "OUTCOME_0/1",

                    "op_yes": op_yes,
                    "op_no": op_no,
                    "pm_yes": str(clob[pm_yes_idx]),
                    "pm_no": str(clob[pm_no_idx]),

                    "pm_yes_label": outcomes[pm_yes_idx] if len(outcomes) == 2 else "PM_OUTCOME_0",
                    "pm_no_label": outcomes[pm_no_idx] if len(outcomes) == 2 else "PM_OUTCOME_1",

                    # URLs
                    "opinion_parent_id": int(opinion_parent) if opinion_parent else None,
                    "pm_event_slug": pm_slug or None,
                        "pm_endDate": (pm.get("endDate") or pm.get("end_date") or item.get("polymarket_event_endDate") or item.get("polymarket_event_end_date")),
                }
            )

        elif mtype == "categorical":
            parent_id = item.get("opinion_market_id")
            pm_event_slug = item.get("polymarket_event_slug") or None

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

                legs.append(
                    {
                        "type": "categorical",
                        "name": name,
                        "candidate": cand,

                        "op_yes": op_yes,
                        "op_no": op_no,
                        "pm_yes": pm_yes,
                        "pm_no": pm_no,

                        "pm_yes_label": "YES",
                        "pm_no_label": "NO",

                        # URLs
                        "opinion_parent_id": int(parent_id) if parent_id else None,
                        "pm_event_slug": pm_event_slug,
                        "pm_endDate": (pm.get("endDate") or pm.get("end_date") or item.get("polymarket_event_endDate") or item.get("polymarket_event_end_date")),
                    }
                )

    return legs

# ===================== 套利判定与输出 =====================
def format_alert(
    leg: Dict[str, Any],
    direction: str,
    sum_cost: float,
    margin: float,
    max_shares: Optional[float],
    deploy_capital: Optional[float],
    pm_price: float,
    pm_size: Optional[float],
    op_price: float,
    op_size: Optional[float],
) -> str:
    op_url = make_opinion_url(leg.get("opinion_parent_id"), leg.get("type", "binary"))
    pm_url = make_polymarket_event_url(leg.get("pm_event_slug"))

    lines = []
    lines.append(f"【套利提醒】{leg['name']} | {leg['candidate']}")
    lines.append(f"方向: {direction}")
    lines.append("")
    lines.append(f"Opinion: {op_url}")
    lines.append(f"Polymarket: {pm_url}")
    lines.append("")
    lines.append(f"套利空间：{margin*100:.2f}%")

    if deploy_capital is not None:
        lines.append(f"可套利资金（最优价档位）：${deploy_capital:.2f}")
    if max_shares is not None:
        lines.append(f"可套利份额（min(size)）：{max_shares:.4f}")
        lines.append(f"预估利润（最优价档位）：${(max_shares*margin):.4f}")

    lines.append("")
    lines.append(f"PM 价格(ask): {pm_price:.4f}, size={pm_size}")
    lines.append(f"OP 价格(ask): {op_price:.4f}, size={op_size}")
    lines.append(f"合计成本: {sum_cost:.4f}")
    return "\n".join(lines)

# ===================== 主循环 =====================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=MARKET_JSON_DEFAULT, help="market_token_pairs.json 路径")

    # 这些你完全可以只改 default，不传命令行参数
    ap.add_argument("--interval", type=float, default=1.5, help="轮询间隔秒")
    ap.add_argument("--delta-cents", type=float, default=1.8, help="阈值点差(美分)。例如 1 表示 sum < 0.99 才提醒")
    ap.add_argument("--cooldown", type=int, default=180, help="同一条机会最短提醒间隔(秒)")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出")

    ap.add_argument("--workers", type=int, default=120, help="Opinion 并发线程数")
    ap.add_argument("--op-qps", type=float, default=10.0, help="Opinion 限速 QPS（线程共享）")
    ap.add_argument("--pm-qps", type=float, default=7.0, help="Polymarket 限速 QPS（/books 批量也算一次）")
    ap.add_argument("--gamma-qps", type=float, default=2.0, help="Gamma 限速 QPS（仅 endDate 缺失时 fallback 用）")


    ap.add_argument("--pm-batch", default=True, action=argparse.BooleanOptionalAction,
                    help="Polymarket 使用 /books 批量（强烈推荐默认开启）")

    # ====== 新增：提醒过滤（默认写死）======
    ap.add_argument("--min-deploy-usd", type=float, default=MIN_DEPLOY_USD_DEFAULT,
                    help="可套利资金低于该值(USD)不提醒")
    ap.add_argument("--max-days-to-expiry", type=int, default=MAX_DAYS_TO_EXPIRY_DEFAULT,
                    help="距离 Polymarket endDate 超过该天数不提醒（事件 endDate）")

    args = ap.parse_args()

    if not os.path.exists(args.json):
        raise SystemExit(f"找不到 {args.json}")

    with open(args.json, "r", encoding="utf-8") as f:
        market_json = json.load(f)

    legs = build_legs(market_json)
    if not legs:
        raise SystemExit("JSON 里没有解析出任何可监控的 token leg")

    delta = args.delta_cents / 100.0
    threshold = 1.0 - delta

    op_keys = get_opinion_keys()
    if not op_keys:
        raise SystemExit("未配置 Opinion API Key：请设置 OPINION_API_KEYS 或 OPINION_API_KEY")

    # 每个 key 一个 limiter：args.op_qps 视为“每个 key 的 QPS”
    op_limiters = [RateLimiter(args.op_qps) for _ in op_keys]

    def pick_key_idx(token_id: str, n: int) -> int:
        if n <= 1:
            return 0
        try:
            # token_id 是大数字字符串，取末 9 位做分配（稳定、够均匀）
            return int(token_id[-9:]) % n
        except Exception:
            return sum(ord(c) for c in token_id) % n


    pm_limiter = RateLimiter(args.pm_qps)
    gamma_limiter = RateLimiter(args.gamma_qps)  # ✅新增


    last_sent: Dict[str, float] = {}

    print(f"=== arb_monitor 启动 ===")
    print(f"Opinion API keys={len(op_keys)} (op_qps per key={args.op_qps})")
    print(f"legs: {len(legs)}, interval={args.interval}s, threshold=sum<{threshold:.4f} (delta={delta:.4f})")
    print(f"min_deploy=${args.min_deploy_usd:.2f}, max_days_to_expiry={args.max_days_to_expiry}d")
    print(f"Opinion workers={args.workers}, op_qps={args.op_qps}, pm_batch={args.pm_batch}, pm_qps={args.pm_qps}\n")

    # main() 里 while True 外面，先建一次
    op_executor = ThreadPoolExecutor(max_workers=args.workers)
    tg_executor = ThreadPoolExecutor(max_workers=4)

    # ===== Warmup：预创建线程 + 在线程内初始化 opinion Session（减少第一轮冷启动抖动）=====
    def _warm_op_thread():
        get_session("opinion")  # 让这个线程创建自己的 requests.Session + pool
        return 1

    warm_futs = [op_executor.submit(_warm_op_thread) for _ in range(args.workers)]
    for f in as_completed(warm_futs):
        f.result()

    # 主线程也顺手把其他 session 建一下（不耗时）
    get_session("poly")
    get_session("gamma")
    get_session("tg")
    # ===== Warmup end =====

    try:
        while True:
            t0 = time.perf_counter()

            # ====== 2) 结束时间过滤（优先用 JSON 里的 pm_endDate；>max_days 不监控）======
            t_filter0 = time.perf_counter()
            active_legs = [leg for leg in legs if leg_is_within_days(leg, args.max_days_to_expiry, gamma_limiter)]
            t_filter = time.perf_counter() - t_filter0

            if not active_legs:
                # 全被过滤了，就等下一轮
                dt = time.time() - t0
                sleep_for = max(0.0, args.interval - dt)

                print(
                f"[ROUND] dt={dt:.3f}s | filter={t_filter:.3f}s | "
                f"op=0.000s | pm=0.000s | arb+tg=0.000s | "
                f"active_legs=0 op_tokens=0 pm_tokens=0 alerts=0 | sleep={sleep_for:.3f}s"
                )

                if args.once:
                    print("=== once 模式结束 ===")
                    return
                time.sleep(sleep_for)
                continue

            opinion_tokens = sorted({leg["op_yes"] for leg in active_legs} | {leg["op_no"] for leg in active_legs})
            poly_tokens = sorted({leg["pm_yes"] for leg in active_legs} | {leg["pm_no"] for leg in active_legs})

            # 1) Opinion（并发 + 限速 + 重试）
            t_op0 = time.perf_counter()
            opinion_books: Dict[str, Dict[str, Optional[float]]] = {}

            futs = {}
            for idx, tid in enumerate(opinion_tokens):
                i = idx % len(op_keys)   # ✅ 强制均匀分配
                futs[op_executor.submit(opinion_fetch_orderbook, tid, op_keys[i], op_limiters[i])] = tid


            for fut in as_completed(futs):
                tid = futs[fut]
                try:
                    data = fut.result()
                    opinion_books[tid] = parse_best_bid_ask(data)
                except Exception as e:
                    print(f"[WARN] Opinion token {tid} orderbook failed: {e}")

            t_op = time.perf_counter() - t_op0

            # 2) Polymarket（批量优先；缺失再补齐）
            t_pm0 = time.perf_counter()
            poly_books: Dict[str, Dict[str, Optional[float]]] = {}
            if args.pm_batch:
                raw = polymarket_fetch_books_batch(poly_tokens, pm_limiter, chunk_size=200)
                for tid, obj in raw.items():
                    try:
                        poly_books[tid] = parse_best_bid_ask(obj)
                    except Exception:
                        pass
            else:
                for tid in poly_tokens:
                    try:
                        obj = polymarket_fetch_book_single(tid, pm_limiter)
                        poly_books[tid] = parse_best_bid_ask(obj)
                    except Exception as e:
                        print(f"[WARN] Polymarket token {tid} /book failed: {e}")
            t_pm = time.perf_counter() - t_pm0

            # 3) 套利
            t_arb0 = time.perf_counter()
            sent_cnt = 0

            for leg in active_legs:

                pm_yes = poly_books.get(leg["pm_yes"])
                pm_no = poly_books.get(leg["pm_no"])
                op_yes = opinion_books.get(leg["op_yes"])
                op_no = opinion_books.get(leg["op_no"])

                # A：PM YES + OP NO
                if pm_yes and op_no:
                    pm_price = pm_yes.get("best_ask")
                    pm_size = pm_yes.get("best_ask_size")
                    op_price = op_no.get("best_ask")
                    op_size = op_no.get("best_ask_size")

                    if pm_price is not None and op_price is not None:
                        sum_cost = pm_price + op_price
                        if sum_cost < threshold:
                            margin = 1.0 - sum_cost
                            max_shares = min2(pm_size, op_size)
                            deploy_capital = (max_shares * sum_cost) if max_shares is not None else None

                            # ====== 1) 小于 $10 不提醒 ======
                            if deploy_capital is None or deploy_capital < args.min_deploy_usd:
                                pass
                            else:
                                key = f"{leg['name']}|{leg['candidate']}|A"
                                now = time.time()
                                if now - last_sent.get(key, 0) >= args.cooldown:
                                    direction = f"买 PM({leg['pm_yes_label']}) + 买 OP(NO)"
                                    msg = format_alert(
                                        leg, direction, sum_cost, margin, max_shares, deploy_capital,
                                        pm_price, pm_size, op_price, op_size
                                    )
                                    tg_executor.submit(tg_send, msg)
                                    sent_cnt += 1
                                    last_sent[key] = now

                # B：PM NO + OP YES
                if pm_no and op_yes:
                    pm_price = pm_no.get("best_ask")
                    pm_size = pm_no.get("best_ask_size")
                    op_price = op_yes.get("best_ask")
                    op_size = op_yes.get("best_ask_size")

                    if pm_price is not None and op_price is not None:
                        sum_cost = pm_price + op_price
                        if sum_cost < threshold:
                            margin = 1.0 - sum_cost
                            max_shares = min2(pm_size, op_size)
                            deploy_capital = (max_shares * sum_cost) if max_shares is not None else None

                            # ====== 1) 小于 $10 不提醒 ======
                            if deploy_capital is None or deploy_capital < args.min_deploy_usd:
                                pass
                            else:
                                key = f"{leg['name']}|{leg['candidate']}|B"
                                now = time.time()
                                if now - last_sent.get(key, 0) >= args.cooldown:
                                    direction = f"买 PM({leg['pm_no_label']}) + 买 OP(YES)"
                                    msg = format_alert(
                                        leg, direction, sum_cost, margin, max_shares, deploy_capital,
                                        pm_price, pm_size, op_price, op_size
                                    )
                                    tg_executor.submit(tg_send, msg)
                                    sent_cnt += 1
                                    last_sent[key] = now
            t_arb = time.perf_counter() - t_arb0    

            dt = time.perf_counter() - t0
            sleep_for = max(0.0, args.interval - dt)

            print(
                f"[ROUND] dt={dt:.3f}s | filter={t_filter:.3f}s | "
                f"op={t_op:.3f}s | pm={t_pm:.3f}s | arb+tg={t_arb:.3f}s | "
                f"active_legs={len(active_legs)} op_tokens={len(opinion_tokens)} pm_tokens={len(poly_tokens)} "
                f"alerts={sent_cnt} | sleep={sleep_for:.3f}s"
            )

            if args.once:
                print("=== once 模式结束 ===")
                return
            time.sleep(sleep_for)

    except KeyboardInterrupt:
        print("\n🛑 Ctrl+C 停止。")

    finally:
        op_executor.shutdown(wait=True)
        tg_executor.shutdown(wait=True)

if __name__ == "__main__":
    main()
