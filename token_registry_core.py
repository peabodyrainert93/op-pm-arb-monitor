# token_registry_core.py
import os
import json
import re
import time
import random
import threading
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()

SCHEMA_VERSION = 6  # v6：修正 categorical 下每个 polymarket 子市场 endDate（按 market 级别写入）

# requests 超时：(connect_timeout, read_timeout)
# 旧版 read_timeout=10 在并发时更容易触发 Read timed out，这里给更宽松的默认值。
HTTP_TIMEOUT = (6, 25)

OPINION_BASE_URL = "https://openapi.opinion.trade/openapi"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

def _parse_opinion_keys() -> List[str]:
    """支持多条 Opinion API Key：

    - 推荐：OPINION_API_KEYS=key1,key2,key3
    - 兼容旧写法：OPINION_API_KEY=key1
    """
    raw = (os.getenv("OPINION_API_KEYS") or "").strip()
    if raw:
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        return keys
    legacy = (os.getenv("OPINION_API_KEY") or "").strip()
    return [legacy] if legacy else []


OPINION_API_KEYS: List[str] = _parse_opinion_keys()


class TokenFetcherError(Exception):
    pass


# =========================
# Thread-local Session (线程安全)
# =========================
_thread_local = threading.local()


def _get_session() -> requests.Session:
    if getattr(_thread_local, "session", None) is None:
        s = requests.Session()
        _thread_local.session = s
    return _thread_local.session


# =========================
# Per-host Rate Limiter
# =========================
def _env_float(name: str, default: float) -> float:
    v = os.getenv(name, "").strip()
    if not v:
        return default
    try:
        return float(v)
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except Exception:
        return default


class HostRateLimiter:
    def __init__(self, host_min_interval: Dict[str, float]):
        self.host_min_interval = host_min_interval
        self._lock = threading.Lock()
        self._next_allowed: Dict[str, float] = {}

    def wait(self, url: str):
        host = urlparse(url).netloc.lower()
        interval = self.host_min_interval.get(host, 0.0)
        if interval <= 0:
            return

        now = time.monotonic()
        with self._lock:
            nxt = self._next_allowed.get(host, now)
            scheduled = max(now, nxt)
            self._next_allowed[host] = scheduled + interval

        sleep_s = scheduled - now
        if sleep_s > 0:
            time.sleep(sleep_s)


DEFAULT_OPINION_MIN_INTERVAL = _env_float("OPINION_MIN_INTERVAL", 0.25)
DEFAULT_GAMMA_MIN_INTERVAL = _env_float("GAMMA_MIN_INTERVAL", 0.25)

# 请求超时：requests 支持 (connect_timeout, read_timeout)
# 你可以在 .env 里设置：HTTP_CONNECT_TIMEOUT / HTTP_READ_TIMEOUT
HTTP_CONNECT_TIMEOUT = _env_float("HTTP_CONNECT_TIMEOUT", 6.0)
HTTP_READ_TIMEOUT = _env_float("HTTP_READ_TIMEOUT", 20.0)
HTTP_TIMEOUT = (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)

_RATE_LIMITER = HostRateLimiter(
    {
        "openapi.opinion.trade": DEFAULT_OPINION_MIN_INTERVAL,
        "gamma-api.polymarket.com": DEFAULT_GAMMA_MIN_INTERVAL,
    }
)

MAX_WORKERS = _env_int("MAX_WORKERS", 8)

MAX_RETRIES = _env_int("HTTP_MAX_RETRIES", 4)
BACKOFF_BASE = _env_float("HTTP_BACKOFF_BASE", 0.6)


# =========================
# Common helpers
# =========================
def _require_opinion_key():
    if not OPINION_API_KEYS:
        raise TokenFetcherError("未配置 Opinion API key：请在 .env 中添加 OPINION_API_KEY 或 OPINION_API_KEYS")


def _pick_opinion_key() -> str:
    """线程内轮询选择 key，避免所有线程都打到同一条 key。"""
    if not OPINION_API_KEYS:
        return ""
    if len(OPINION_API_KEYS) == 1:
        return OPINION_API_KEYS[0]
    idx = getattr(_thread_local, "op_key_idx", 0)
    k = OPINION_API_KEYS[idx % len(OPINION_API_KEYS)]
    setattr(_thread_local, "op_key_idx", idx + 1)
    return k


def _opinion_headers() -> Dict[str, str]:
    _require_opinion_key()
    return {"apikey": _pick_opinion_key(), "Accept": "application/json"}


def extract_opinion_market_id_from_url(opinion_url: str) -> int:
    parsed = urlparse(opinion_url)
    qs = parse_qs(parsed.query)
    for key in ("topicId", "marketId", "id"):
        if key in qs and qs[key]:
            try:
                return int(qs[key][0])
            except ValueError:
                pass
    raise TokenFetcherError(f"无法从 Opinion URL 解析 marketId: {opinion_url}")


def extract_polymarket_slug_from_url(poly_url: str) -> str:
    parsed = urlparse(poly_url)
    parts = parsed.path.strip("/").split("/")
    if not parts:
        raise TokenFetcherError(f"无法从 Polymarket URL 解析 slug: {poly_url}")
    return parts[-1]


def _norm_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("“", '"').replace("”", '"').replace("’", "'")
    s = s.strip('"').strip("'")
    # 把各种箭头统一成空格（key 里用 digits-only/word-only 再补）
    s = s.replace("↑", " ").replace("↓", " ").replace("→", " ").replace("–", "-")
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _digits_only(s: str) -> str:
    return re.sub(r"\D+", "", s or "")


def _strip_years(s: str) -> str:
    # 去掉 4 位年份（1900-2099）
    s = re.sub(r"\b(19|20)\d{2}\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _strip_rate_words(s: str) -> str:
    """
    把 "decrease rates" / "increase interest rates" 这类尾巴去掉，
    让它能匹配到 "decrease" / "increase"
    """
    s2 = (s or "").strip()
    # 注意：这里处理的是“已经 norm 过”的文本（全小写、无标点）
    s2 = re.sub(r"\binterest\s+rates?\b", " ", s2)
    s2 = re.sub(r"\brates?\b", " ", s2)
    s2 = re.sub(r"\brate\b", " ", s2)
    s2 = re.sub(r"\s+", " ", s2).strip()
    return s2

def _expand_compact_number_to_int(s: str) -> Optional[int]:
    """
    把 150k / 1.5k / 2m / 0.25m / 1b 解析成整数：
      k=1_000, m=1_000_000, b=1_000_000_000
    返回 None 表示无法解析
    """
    if not s:
        return None
    t = s.strip().lower()
    # 去掉货币符号和逗号
    t = t.replace("$", "").replace(",", "").replace("，", "")
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*([kmb])\b", t)
    if not m:
        return None
    num = float(m.group(1))
    suf = m.group(2)
    mul = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suf]
    return int(round(num * mul))


_MONTHS = (
    "january","february","march","april","may","june",
    "july","august","september","october","november","december",
)


def _extract_month_day(text: str) -> Optional[str]:
    t = (text or "").strip().lower()
    m = re.search(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})\b", t)
    if not m:
        return None
    return f"{m.group(1)} {int(m.group(2))}"


def _extract_range(text: str) -> Optional[str]:
    t = (text or "").replace(",", "")
    m = re.search(r"(\d[\d]*)\s*(?:-|–|to)\s*(\d[\d]*)", t, flags=re.IGNORECASE)
    if not m:
        return None
    a = m.group(1)
    b = m.group(2)
    if a and b:
        return f"{a}-{b}"
    return None


def _extract_directional_threshold(text: str) -> Optional[Tuple[str, int]]:
    """从文本中尝试提取方向(ge/le/gt/lt) + 数字。

    支持：
      - 符号：>= <= > <
      - 词：reach/hit/at least/above/over  vs  dip/below/under/less than
      - 箭头：↑ / ↓
    """
    raw = (text or "")
    t = raw.replace(",", "")

    # 1) 符号优先
    m = re.search(r"(>=|<=|>|<)\s*\$?\s*(\d+(?:\.\d+)?)", t)
    if m:
        op = m.group(1)
        num = float(m.group(2))
        if op == ">=":
            return ("ge", int(round(num)))
        if op == "<=":
            return ("le", int(round(num)))
        if op == ">":
            return ("gt", int(round(num)))
        if op == "<":
            return ("lt", int(round(num)))

    # 2) 先抓一个“像价格/阈值”的数字
    num_s = None
    m2 = re.search(r"\$\s*(\d{2,}(?:\.\d+)?)", t)
    if m2:
        num_s = m2.group(1)
    else:
        m3 = re.search(r"\b(\d{2,}(?:\.\d+)?)\b", t)
        if m3:
            num_s = m3.group(1)
    if not num_s:
        return None
    num_i = int(round(float(num_s)))

    tl = t.lower()

    # 3) 箭头/方向词
    if "↑" in raw or re.search(r"\b(up|reach|hit|at least|above|over|greater than|more than)\b", tl):
        return ("ge", num_i)
    if "↓" in raw or re.search(r"\b(down|dip|below|under|less than|at most)\b", tl):
        return ("le", num_i)

    return None


def _make_keys(label: str, extra_text: Optional[str] = None) -> List[str]:
    """给一个 candidate 生成多组可匹配 key（更强健）。"""
    keys = set()
    if not label and not extra_text:
        return []

    texts = []
    if label:
        texts.append(label)
    if extra_text:
        texts.append(extra_text)

    for raw in texts:
        raw = (raw or "").strip()
        if not raw:
            continue

        # 1) 常规 & 去逗号
        base = _norm_text(raw)
        if base:
            keys.add(base)

        no_comma = _norm_text(raw.replace(",", "").replace("，", ""))
        if no_comma:
            keys.add(no_comma)

        # 2) 去年份（December 15, 2025 -> December 15）
        for k0 in (base, no_comma):
            if k0:
                k = _strip_years(k0)
                if k:
                    keys.add(k)

        # 3) 去 rates/interest rates
        for k0 in list(keys):
            k1 = _strip_rate_words(k0)
            if k1:
                keys.add(k1)

        # 4) 月-日
        md = _extract_month_day(raw)
        if md:
            keys.add(md)

        # 5) 数字/紧凑数字
        digits = _digits_only(raw)
        if len(digits) >= 3:
            keys.add(digits)

        expanded = _expand_compact_number_to_int(raw)
        if expanded is not None and expanded >= 100:
            keys.add(str(expanded))

        # 6) 区间（280–295 -> 280-295）
        rg = _extract_range(raw)
        if rg:
            keys.add(rg)

        # 7) 方向阈值（↑105k / below 4000 / >$500 等）
        th = _extract_directional_threshold(raw)
        if th:
            op, num = th
            keys.add(f"{op}_{num}")
            keys.add(str(num))

    # 8) Increase/Decrease 等候选（兼容 "Decrease rates" 等）
    for k0 in list(keys):
        m = re.fullmatch(r"(increase|decrease)(\s+rates?)?", k0)
        if m:
            keys.add(m.group(1))

        # hold / no change / unchanged
        if k0 in {"hold", "no change", "unchanged", "nochange"}:
            keys.add("hold")
            keys.add("no change")

    # 9) 同义（another/other）
    if "another game" in keys or "another" in keys:
        keys.add("other")
    if "other" in keys:
        keys.add("another game")

    return [k for k in keys if k]


def _key_weight(k: str) -> int:
    k = (k or "").strip()
    if not k:
        return 0
    if re.match(r"^(ge|gt|le|lt)_\d{2,}$", k):
        return 12
    if re.match(r"^\d{3,}$", k):
        return 10
    if re.match(r"^\d{2,}-\d{2,}$", k):
        return 9
    if re.match(r"^(" + "|".join(_MONTHS) + r")\s+\d{1,2}$", k):
        return 7
    if k in {"increase", "decrease", "hold", "no change"}:
        return 3
    if k in {"yes", "no"}:
        return 2
    return 1


def _score_keys(keys: set) -> int:
    return sum(_key_weight(k) for k in keys)



def _request_with_retry(method: str, url: str, *, headers=None, params=None, timeout=HTTP_TIMEOUT) -> requests.Response:
    sess = _get_session()
    last_exc = None
    retryable_status = {429, 500, 502, 503, 504}

    for attempt in range(MAX_RETRIES):
        try:
            _RATE_LIMITER.wait(url)
            resp = sess.request(method, url, headers=headers, params=params, timeout=timeout)

            if resp.status_code == 200:
                return resp

            if resp.status_code == 403 and attempt == 0:
                time.sleep(BACKOFF_BASE + random.uniform(0, 0.25))
                continue

            if resp.status_code in retryable_status:
                time.sleep((BACKOFF_BASE * (2 ** attempt)) + random.uniform(0, 0.25))
                continue

            return resp

        except (requests.exceptions.RequestException, ConnectionError) as e:
            last_exc = e
            time.sleep((BACKOFF_BASE * (2 ** attempt)) + random.uniform(0, 0.25))

    if last_exc:
        raise TokenFetcherError(f"请求失败（重试后仍失败）: {url} ; err={last_exc}")
    raise TokenFetcherError(f"请求失败（重试后仍失败）: {url}")


def _http_get_json(url: str, headers=None, params=None, timeout=HTTP_TIMEOUT) -> Any:
    resp = _request_with_retry("GET", url, headers=headers, params=params, timeout=timeout)
    if resp.status_code != 200:
        raise TokenFetcherError(f"HTTP {resp.status_code} for {url}: {resp.text[:300]}")
    try:
        return resp.json()
    except Exception:
        raise TokenFetcherError(f"非 JSON 响应: {url}: {resp.text[:300]}")


# =========================
# Cache helpers
# =========================
def _cache_key_from_cfg(cfg: Dict[str, str]) -> str:
    mtype = cfg.get("type", "binary")
    op_id = extract_opinion_market_id_from_url(cfg["opinion_url"])
    slug = cfg.get("polymarket_slug") or extract_polymarket_slug_from_url(cfg["polymarket_url"])
    return f"{mtype}|{op_id}|{slug}"


def _cache_key_from_entry(entry: Dict[str, Any]) -> Optional[str]:
    try:
        mtype = entry.get("type")
        if mtype == "binary":
            op_id = entry.get("opinion", {}).get("market_id")
            slug = entry.get("polymarket", {}).get("slug")
            if op_id and slug:
                return f"binary|{int(op_id)}|{slug}"
        elif mtype == "categorical":
            op_id = entry.get("opinion_market_id") or entry.get("opinion", {}).get("main_market_id")
            slug = entry.get("polymarket_event_slug") or entry.get("polymarket", {}).get("slug")
            if op_id and slug:
                return f"categorical|{int(op_id)}|{slug}"
    except Exception:
        return None
    return None


def load_cache(cache_path: str) -> Dict[str, Dict[str, Any]]:
    if not cache_path or not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return {}
    except Exception:
        return {}

    cache: Dict[str, Dict[str, Any]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        k = _cache_key_from_entry(entry)
        if k:
            cache[k] = entry
    return cache


def _entry_is_usable(entry: Dict[str, Any]) -> bool:
    try:
        # ✅ schema 版本不对就强制刷新
        if entry.get("schema_version") != SCHEMA_VERSION:
            return False

        if entry.get("type") == "binary":
            op = entry.get("opinion", {})
            pm = entry.get("polymarket", {})
            return bool(op.get("yes_token_id") and op.get("no_token_id") and pm.get("clob_token_ids"))

        if entry.get("type") == "categorical":
            return isinstance(entry.get("pairs"), list)

    except Exception:
        return False
    return False



# =========================
# Opinion
# =========================
def _opinion_get_market_generic(market_id: int) -> Dict[str, Any]:
    _require_opinion_key()
    headers = _opinion_headers()

    urls = [
        f"{OPINION_BASE_URL}/market/binary/{market_id}",
        f"{OPINION_BASE_URL}/market/{market_id}",
    ]

    last_err = None
    for url in urls:
        try:
            data = _http_get_json(url, headers=headers, timeout=HTTP_TIMEOUT)
        except Exception as e:
            last_err = str(e)
            continue

        code = data.get("code", data.get("errno"))
        if code not in (0, None):
            last_err = f"Opinion 返回错误: {data}"
            continue

        result = data.get("result") or {}
        market = result.get("data") or result
        if market:
            return market

        last_err = f"Opinion 返回结构异常: {data}"

    raise TokenFetcherError(last_err or f"Opinion 市场详情获取失败: {market_id}")


def _opinion_get_categorical_market_by_id(market_id: int) -> Dict[str, Any]:
    _require_opinion_key()
    url = f"{OPINION_BASE_URL}/market/categorical/{market_id}"
    headers = _opinion_headers()

    data = _http_get_json(url, headers=headers, timeout=HTTP_TIMEOUT)
    code = data.get("code", data.get("errno"))
    if code not in (0, None):
        raise TokenFetcherError(f"Opinion 返回错误: {data}")

    result = data.get("result") or {}
    market = result.get("data") or result
    return market


def opinion_fetch_binary_tokens(market_id: int) -> Dict[str, Any]:
    market = _opinion_get_market_generic(market_id)
    return {
        "market_id": market.get("marketId") or market_id,
        "title": market.get("marketTitle") or "",
        "yes_token_id": market.get("yesTokenId"),
        "no_token_id": market.get("noTokenId"),
    }


def opinion_fetch_categorical_children(market_id: int) -> List[Dict[str, Any]]:
    market = _opinion_get_categorical_market_by_id(market_id)
    children = market.get("childMarkets") or []
    out: List[Dict[str, Any]] = []
    for c in children:
        out.append(
            {
                "market_id": c.get("marketId"),
                "title": c.get("marketTitle") or "",
                "yes_token_id": c.get("yesTokenId"),
                "no_token_id": c.get("noTokenId"),
            }
        )
    return out


# =========================
# Polymarket (Gamma)
# =========================
def _gamma_headers() -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }


def _parse_json_list(v: Any) -> List[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return []


def gamma_get_event_by_slug(slug: str) -> Dict[str, Any]:
    headers = _gamma_headers()

    def get_event(s: str) -> Optional[Dict[str, Any]]:
        url = f"{GAMMA_BASE_URL}/events/slug/{s}"
        resp = _request_with_retry("GET", url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return None
        raise TokenFetcherError(f"Polymarket /events/slug/{s} HTTP {resp.status_code}: {resp.text[:300]}")

    m = re.search(r"(.+)-\d+$", slug)
    alt_slug = m.group(1) if m else None

    ev = get_event(slug)
    if ev:
        return ev

    if alt_slug:
        ev2 = get_event(alt_slug)
        if ev2:
            return ev2

    raise TokenFetcherError(f"Polymarket event slug '{slug}'（及其 alt）404 / 不存在")


def gamma_get_market_by_slug_or_event(slug: str) -> Dict[str, Any]:
    headers = _gamma_headers()

    def get_market(s: str) -> Optional[Dict[str, Any]]:
        url = f"{GAMMA_BASE_URL}/markets/slug/{s}"
        resp = _request_with_retry("GET", url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return None
        raise TokenFetcherError(f"Polymarket /markets/slug/{s} HTTP {resp.status_code}: {resp.text[:300]}")

    def alt(s: str) -> Optional[str]:
        m = re.search(r"(.+)-\d+$", s)
        return m.group(1) if m else None

    m1 = get_market(slug)
    if m1:
        return m1

    a = alt(slug)
    if a:
        m2 = get_market(a)
        if m2:
            return m2

    # fallback: event -> pick first market
    ev = gamma_get_event_by_slug(slug)
    markets = ev.get("markets") or []
    if not markets:
        raise TokenFetcherError(f"events/slug/{slug} 返回成功，但 markets 为空")
    return markets[0]


def gamma_get_market_by_id(market_id: str) -> Dict[str, Any]:
    """按 market id 获取完整 market（用于补齐 endDate 等字段）。"""
    headers = _gamma_headers()
    mid = str(market_id)
    url = f"{GAMMA_BASE_URL}/markets/{mid}"
    resp = _request_with_retry("GET", url, headers=headers, timeout=HTTP_TIMEOUT)
    if resp.status_code == 200:
        return resp.json()
    raise TokenFetcherError(f"Polymarket /markets/{mid} HTTP {resp.status_code}: {resp.text[:300]}")


def gamma_parse_yes_no_from_market(m: Dict[str, Any]) -> Dict[str, Any]:
    outcomes = _parse_json_list(m.get("outcomes"))
    clob_ids = _parse_json_list(m.get("clobTokenIds"))

    yes_token_id = None
    no_token_id = None

    if len(outcomes) == 2 and len(clob_ids) == 2:
        o0 = str(outcomes[0]).strip().lower()
        o1 = str(outcomes[1]).strip().lower()

        # 默认按 [0]=YES [1]=NO，但如果反过来就交换
        if o0 == "no" and o1 == "yes":
            yes_token_id = str(clob_ids[1])
            no_token_id = str(clob_ids[0])
        else:
            # 常见情况：["Yes","No"] 或 非 yes/no（Up/Down）就保持 0/1
            yes_token_id = str(clob_ids[0])
            no_token_id = str(clob_ids[1])

    return {
        "market_id": str(m.get("id") or ""),
        "question": m.get("question") or "",
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        "outcomes": outcomes,
        "clob_token_ids": clob_ids,
    }


def _is_placeholder_candidate(name: str) -> bool:
    """
    更通用的占位过滤：
      - Game C / Movie D / Team A / Option B / Candidate E ...
    """
    n = (name or "").strip()
    if bool(re.fullmatch(r"(Game|Movie|Team|Option|Candidate|Player|Item)\s+[A-Z]", n, flags=re.IGNORECASE)):
        return True

    # 这类兜底项也通常会造成噪音/误匹配
    if re.search(r"\banother game\b", n, re.IGNORECASE):
        return True

    return False

def gamma_extract_candidate_from_question(q: str) -> str:
    q = (q or "").strip()

    # 优先：单引号里的候选项（Steam Awards 这种）
    m = re.search(r"'([^']+)'", q)
    if m:
        return m.group(1).strip()

    # 兜底：another game
    if re.search(r"\banother game\b", q, re.IGNORECASE):
        return "Another game"

    # 最后兜底：返回原问题
    return q



def gamma_extract_candidate_from_market(raw_market: Dict[str, Any], question: str) -> str:
    """
    categorical 下优先用 groupItemTitle（这是你要的关键）；
    没有 groupItemTitle 再 fallback 解析 question。
    """
    # 1) groupItemTitle（你在文档里看到的）
    for k in ("groupItemTitle", "group_item_title", "groupTitle", "group_item_title_text"):
        v = raw_market.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    q = (question or "").strip()

    # 2) 原来的：引号里的候选（Steam Awards 这种）
    m = re.search(r"'([^']+)'", q)
    if m:
        return m.group(1).strip()

    # 3) “another game”
    if re.search(r"\banother\b.*\bgame\b", q, re.IGNORECASE):
        return "Another game"

    # 4) 利率 Increase/Decrease/No change & bps
    if re.search(r"\bdecreases?\b|\bcuts?\b", q, re.IGNORECASE):
        m2 = re.search(r"by\s+(\d+\+?)\s*bps", q, re.IGNORECASE)
        return f"{m2.group(1)} bps decrease" if m2 else "Decrease"
    if re.search(r"\bincreases?\b|\bhikes?\b", q, re.IGNORECASE):
        m2 = re.search(r"by\s+(\d+\+?)\s*bps", q, re.IGNORECASE)
        return f"{m2.group(1)} bps increase" if m2 else "Increase"
    if re.search(r"\bno\s+change\b|\bunchanged\b|\bkeeps?\b", q, re.IGNORECASE):
        return "No change"

    # 5) close at range: $280–295
    m = re.search(r"close\s+at\s+\$?([0-9][0-9,]*)\s*(?:–|-|to)\s*\$?([0-9][0-9,]*)", q, re.IGNORECASE)
    if m:
        a = m.group(1).replace(",", "")
        b = m.group(2).replace(",", "")
        return f"${a}–{b}"

    # 6) close below/above: <$4000 / >$500
    m = re.search(r"close\s+below\s+\$?([0-9][0-9,]*)", q, re.IGNORECASE)
    if m:
        return f"<${m.group(1)}"
    m = re.search(r"close\s+above\s+\$?([0-9][0-9,]*)", q, re.IGNORECASE)
    if m:
        return f">${m.group(1)}"
    m = re.search(r"close\s+at\s+<\s*\$?([0-9][0-9,]*)", q, re.IGNORECASE)
    if m:
        return f"<${m.group(1)}"
    m = re.search(r"close\s+at\s+>\s*\$?([0-9][0-9,]*)", q, re.IGNORECASE)
    if m:
        return f">${m.group(1)}"

    # 7) hit/reach/dip：↑ 105,000 / ↓ 80,000 / ↑$5,000
    m = re.search(r"(reach|hit)\s+\$?([0-9][0-9,]*)", q, re.IGNORECASE)
    if m:
        # 带 $ 的更像 ↑$5000
        if "$" in q[m.start():m.end()+1]:
            return f"↑${m.group(2)}"
        return f"↑ {m.group(2)}"
    m = re.search(r"dip\s+to\s+\$?([0-9][0-9,]*)", q, re.IGNORECASE)
    if m:
        return f"↓ {m.group(1)}"

    # 兜底：就用 question
    return q or ""


def gamma_event_to_candidate_markets(event: Dict[str, Any]) -> Tuple[str, str, Optional[str], List[Dict[str, Any]]]:
    event_id = str(event.get("id") or "")
    event_title = event.get("title") or ""
    # 事件级 endDate（有些 event 只有一个时间；但也存在“同一 event 下多个 market 不同 endDate”的情况）
    event_end_date = event.get("endDate")
    markets = event.get("markets") or []

    # events/slug 有时不会把每个 market 的 endDate 带全（但 /markets/{id} 会有）。
    # 为了避免像 TikTok 这种“同一 event 下不同 market 不同截止时间”被写成同一个时间，
    # 这里按 market_id 补齐 endDate。
    enddate_cache: Dict[str, Optional[str]] = {}

    out: List[Dict[str, Any]] = []
    for m in markets:
        parsed = gamma_parse_yes_no_from_market(m)
        # ✅ 优先用 groupItemTitle（它通常就是用户在前端看到的选项文案），匹配 Opinion 的 candidate 更稳。
        cand = (m.get("groupItemTitle") or m.get("group_item_title") or "").strip()
        if not cand:
            cand = gamma_extract_candidate_from_question(parsed.get("question") or "")
        parsed["candidate"] = cand

        # ✅ 每个 market 自己的 endDate（如果 events/slug 里缺失，就按 market_id 再查一次 /markets/{id}）
        m_end = m.get("endDate") or m.get("end_date")
        if not m_end:
            mid = str(parsed.get("market_id") or "")
            if mid:
                if mid not in enddate_cache:
                    try:
                        full = gamma_get_market_by_id(mid)
                        enddate_cache[mid] = full.get("endDate") or full.get("end_date")
                    except Exception:
                        enddate_cache[mid] = None
                m_end = enddate_cache.get(mid)
        parsed["endDate"] = m_end or event_end_date

        # placeholder 标记：后续 unmatched 默认不写入，减少噪音
        parsed["placeholder"] = bool(
            _is_placeholder_candidate(parsed.get("candidate") or "")
            or re.search(r"\banother\s+game\b", parsed.get("candidate") or "", re.IGNORECASE)
        )

        # 过滤占位项：Game C / Game D / ...
        if parsed.get("placeholder"):
            continue

        out.append(parsed)

    return event_id, event_title, event_end_date, out



# =========================
# Builders
# =========================
def build_entry_from_urls(cfg: Dict[str, str]) -> Dict[str, Any]:
    name = cfg.get("name") or "UNNAMED"
    mtype = cfg.get("type", "binary")
    opinion_url = cfg["opinion_url"]
    poly_url = cfg["polymarket_url"]

    opinion_market_id = extract_opinion_market_id_from_url(opinion_url)
    slug = cfg.get("polymarket_slug") or extract_polymarket_slug_from_url(poly_url)

    if mtype == "binary":
        op = opinion_fetch_binary_tokens(opinion_market_id)
        pm_market = gamma_get_market_by_slug_or_event(slug)
        pm_parsed = gamma_parse_yes_no_from_market(pm_market)

        return {
            "schema_version": SCHEMA_VERSION,
            "name": name,
            "type": "binary",
            "opinion": {
                "market_id": op["market_id"],
                "yes_token_id": op["yes_token_id"],
                "no_token_id": op["no_token_id"],
            },
            "polymarket": {
                "slug": slug,
                "question": pm_parsed.get("question") or "",
                "outcomes": pm_parsed.get("outcomes") or _parse_json_list(pm_market.get("outcomes")),
                "clob_token_ids": pm_parsed.get("clob_token_ids") or _parse_json_list(pm_market.get("clobTokenIds")),
                "endDate": pm_market.get("endDate") or pm_market.get("end_date"),
            },
        }

    if mtype == "categorical":
        op_children = opinion_fetch_categorical_children(opinion_market_id)

        ev = gamma_get_event_by_slug(slug)
        ev_id, ev_title, ev_end_date, pm_candidate_markets = gamma_event_to_candidate_markets(ev)

        # Opinion：每个 candidate 生成多个 key（title 里经常就是选项文本）
        op_items: List[Dict[str, Any]] = []
        for c in op_children:
            title = c.get("title") or ""
            keys = set(_make_keys(title))
            op_items.append({"child": c, "keys": keys, "norm": _norm_text(title)})

        # Polymarket：提前算好 key 集合，方便打分匹配
        pm_items: List[Dict[str, Any]] = []
        for m in pm_candidate_markets:
            if m.get("placeholder"):
                continue
            cand = m.get("candidate") or ""
            q = m.get("question") or ""
            keys = set(_make_keys(cand, extra_text=q))
            pm_items.append({"m": m, "keys": keys, "norm": _norm_text(cand)})

        pairs: List[Dict[str, Any]] = []
        used_pm_market_ids: set = set()

        # 逐个 opinion candidate 找“最佳” polymarket 子市场（按 key overlap 打分）
        for it in op_items:
            oc = it["child"]
            best_pm: Optional[Dict[str, Any]] = None
            best_score = 0

            for cand in pm_items:
                pm = cand["m"]
                mid = pm.get("market_id")
                if not mid or mid in used_pm_market_ids:
                    continue
                if not (pm.get("yes_token_id") and pm.get("no_token_id")):
                    continue

                inter = it["keys"].intersection(cand["keys"])
                if not inter:
                    continue

                score = _score_keys(inter)
                # tie-break：候选名完全一致优先
                if score > best_score or (score == best_score and it["norm"] and it["norm"] == cand["norm"]):
                    best_score = score
                    best_pm = pm

            if best_pm:
                used_pm_market_ids.add(best_pm["market_id"])
                pairs.append(
                    {
                        "candidate": oc.get("title") or "",
                        "opinion": {
                            "market_id": oc.get("market_id"),
                            "yes_token_id": oc.get("yes_token_id"),
                            "no_token_id": oc.get("no_token_id"),
                        },
                        "polymarket": {
                            "market_id": best_pm.get("market_id"),
                            "question": best_pm.get("question") or "",
                            "candidate": best_pm.get("candidate") or "",
                            "yes_token_id": best_pm.get("yes_token_id"),
                            "no_token_id": best_pm.get("no_token_id"),
                            # ✅ 重点：每个子市场使用它自己的 endDate；如果缺失再退回 event_endDate
                            "endDate": best_pm.get("endDate") or ev_end_date,
                        },
                    }
                )

        # unmatched opinion
        matched_op_ids = {p["opinion"]["market_id"] for p in pairs}
        unmatched_opinion: List[Dict[str, Any]] = []
        for c in op_children:
            if c.get("market_id") not in matched_op_ids:
                unmatched_opinion.append(
                    {
                        "market_id": c.get("market_id"),
                        "candidate": c.get("title") or "",
                        "yes_token_id": c.get("yes_token_id"),
                        "no_token_id": c.get("no_token_id"),
                    }
                )

        # unmatched polymarket（默认不写 placeholder=true）
        unmatched_polymarket: List[Dict[str, Any]] = []
        for cand in pm_items:
            pm = cand["m"]
            mid = pm.get("market_id")
            if not mid or mid in used_pm_market_ids:
                continue
            if pm.get("placeholder"):
                continue
            if _is_placeholder_candidate(pm.get("candidate") or ""):
                continue
            unmatched_polymarket.append(
                {
                    "market_id": pm.get("market_id"),
                    "question": pm.get("question") or "",
                    "candidate": pm.get("candidate") or "",
                    "yes_token_id": pm.get("yes_token_id"),
                    "no_token_id": pm.get("no_token_id"),
                    "endDate": pm.get("endDate") or ev_end_date,
                }
            )

        return {
            "schema_version": SCHEMA_VERSION,              # ✅ 顶层加这里
            "name": name,
            "type": "categorical",
            "opinion_market_id": opinion_market_id,
            "polymarket_event_slug": slug,
            "polymarket_event_id": ev_id,
            "polymarket_event_title": ev_title,
            "polymarket_event_endDate": ev_end_date,       # ✅ 顶层加这里
            "pairs": pairs,
            "unmatched_opinion": unmatched_opinion,
            "unmatched_polymarket": unmatched_polymarket,
        }



    raise TokenFetcherError(f"不支持的 type={mtype} (只支持 binary / categorical)")


def build_all(
    url_pairs: List[Dict[str, str]],
    cache_path: Optional[str] = None,
    refresh: bool = False,
    keep_cache_on_error: bool = True,
) -> List[Dict[str, Any]]:
    cache: Dict[str, Dict[str, Any]] = load_cache(cache_path) if (cache_path and not refresh) else {}
    results_by_index: List[Optional[Dict[str, Any]]] = [None] * len(url_pairs)
    tasks: List[Tuple[int, str, Dict[str, str]]] = []

    for i, cfg in enumerate(url_pairs):
        name = cfg.get("name", "UNNAMED")

        try:
            key = _cache_key_from_cfg(cfg)
        except Exception as e:
            print(f"处理：{name}")
            print(f"  ❌ 失败：无法生成缓存 key：{e}\n")
            continue

        if (not refresh) and key in cache and _entry_is_usable(cache[key]):
            cached = cache[key]
            cached["name"] = name
            results_by_index[i] = cached
            print(f"处理：{name}")
            print("  ✅ 使用缓存（跳过 API 请求）\n")
        else:
            tasks.append((i, key, cfg))

    if not tasks:
        return [r for r in results_by_index if r is not None]

    print(f"=== 并发抓取开始：{len(tasks)} 个任务，MAX_WORKERS={MAX_WORKERS} ===\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        future_map = {}
        for (i, key, cfg) in tasks:
            future = ex.submit(build_entry_from_urls, cfg)
            future_map[future] = (i, key, cfg)

        for fut in as_completed(future_map):
            i, key, cfg = future_map[fut]
            name = cfg.get("name", "UNNAMED")

            try:
                entry = fut.result()
                results_by_index[i] = entry
                cache[key] = entry
                print(f"处理：{name}")
                print("  ✅ 成功（并发抓取 & 已更新缓存）\n")

            except Exception as e:
                if (not refresh) and keep_cache_on_error and key in cache and _entry_is_usable(cache[key]):
                    cached = cache[key]
                    cached["name"] = name
                    results_by_index[i] = cached
                    print(f"处理：{name}")
                    print(f"  ⚠️ 抓取失败但保留旧缓存：{e}\n")
                else:
                    print(f"处理：{name}")
                    print(f"  ❌ 失败：{e}\n")

    return [r for r in results_by_index if r is not None]


def write_market_token_pairs_json(results: List[Dict[str, Any]], out_path: str) -> None:
    dir_ = os.path.dirname(out_path)
    if dir_:
        os.makedirs(dir_, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
