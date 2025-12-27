# run_token_registry.py
import os
import argparse

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=None, help="线程数 MAX_WORKERS")
    ap.add_argument("--opinion-interval", type=float, default=None, help="Opinion 每次请求最小间隔（秒）")
    ap.add_argument("--gamma-interval", type=float, default=None, help="Gamma 每次请求最小间隔（秒）")
    ap.add_argument("--retries", type=int, default=None, help="HTTP 最大重试次数")
    ap.add_argument("--backoff", type=float, default=None, help="退避基数秒（越大越保守）")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存，强制重新抓取")
    ap.add_argument("--keep-expired", action="store_true", help="不删除已过期市场（默认会清理）")
    ap.add_argument("--expiry-grace-hours", type=float, default=12.0, help="过期宽限期（小时），默认12")
    args = ap.parse_args()

    # ✅ 在 import token_registry_core 之前写入环境变量（core 会在 import 时读取）
    if args.workers is not None:
        os.environ["MAX_WORKERS"] = str(args.workers)
    if args.opinion_interval is not None:
        os.environ["OPINION_MIN_INTERVAL"] = str(args.opinion_interval)
    if args.gamma_interval is not None:
        os.environ["GAMMA_MIN_INTERVAL"] = str(args.gamma_interval)
    if args.retries is not None:
        os.environ["HTTP_MAX_RETRIES"] = str(args.retries)
    if args.backoff is not None:
        os.environ["HTTP_BACKOFF_BASE"] = str(args.backoff)

    # 你的 URL_PAIRS_FOR_DEBUG 仍然放在 token_registry.py（不需要动那一段）
    from token_registry import URL_PAIRS_FOR_DEBUG
    import token_registry_core as core

    base_dir = os.path.dirname(__file__)
    out_path = os.path.join(base_dir, "market_token_pairs.json")

    # ✅ 缓存文件就用最终的 JSON 本身（成功的 market 会自动复用）
    results = core.build_all(
        URL_PAIRS_FOR_DEBUG,
        cache_path=out_path,
        refresh=args.refresh,
        keep_cache_on_error=True,
    )
    if not args.keep_expired:
        results = core.prune_expired_markets(
            results,
            grace_seconds=float(args.expiry_grace_hours or 0.0) * 3600.0,
            verbose=True,
        )

    core.write_market_token_pairs_json(results, out_path)

    print(f"=== 已写入 {out_path} ===")

if __name__ == "__main__":
    main()
