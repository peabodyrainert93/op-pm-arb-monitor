"""
token_registry.py

你只需要维护 URL_PAIRS_FOR_DEBUG
逻辑都在 token_registry_core.py 里（支持缓存增量更新）
"""

import os
from typing import List, Dict

from token_registry_core import build_all, write_market_token_pairs_json, TokenFetcherError

# ================== 你要填写的网址就在这里 ==================

URL_PAIRS_FOR_DEBUG: List[Dict[str, str]] = [
    # 例子：Steam Awards（注意：下面的 polymarket_slug 你需要用真正的 slug 替换）
    {
        "name": "Steam Awards Game of the Year",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=151&type=multi",
        "polymarket_url": "https://polymarket.com/event/steam-awards-game-of-the-year-395?tid=1765451110492",    
    },

    {
        "name": "US Fed Rate Decision in January?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=61&type=multi",
        "polymarket_url": "https://polymarket.com/event/fed-decision-in-january?tid=1765640826700",    
    },

    {
        "name": "Bank of Japan decision in December?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=74&type=multi",
        "polymarket_url": "https://polymarket.com/event/bank-of-japan-decision-in-december?tid=1765640885419",    
    },

    {
        "name": "What price will gold close at in 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=62&type=multi",
        "polymarket_url": "https://polymarket.com/event/what-price-will-gold-close-at-in-2025-4000-5000?tid=1765640934862",    
    },

    {
        "name": "Oscars 2026: Best Director Winner",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=138&type=multi",
        "polymarket_url": "https://polymarket.com/event/oscars-2026-best-director-winner?tid=1765641785642",    
    },

    {
        "name": "English Premier League Winner 2026",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=80&type=multi",
        "polymarket_url": "https://polymarket.com/event/english-premier-league-winner?tid=1765641508054",    
    },

    {
        "name": "What price will Bitcoin hit in December?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=145&type=multi",
        "polymarket_url": "https://polymarket.com/event/what-price-will-bitcoin-hit-in-2025?tid=1765641836016",    
    },

    {
        "name": "What will Google (GOOGL) close at in 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=125&type=multi",
        "polymarket_url": "https://polymarket.com/event/what-will-google-googl-close-at-in-2025?tid=1765641895519",    
    },

    {
        "name": "What will Tesla (TSLA) close at in 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=124&type=multi",
        "polymarket_url": "https://polymarket.com/event/what-will-tesla-tsla-close-at-in-2025?tid=1765641940785",    
    },

    {
        "name": "What price will Ethereum hit in 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=64&type=multi",
        "polymarket_url": "https://polymarket.com/event/what-price-will-ethereum-hit-in-2025?tid=1765641994594",    
    },

    {
        "name": "What will Google (GOOGL) hit in December 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=110&type=multi",
        "polymarket_url": "https://polymarket.com/event/what-price-will-googl-hit-in-december-2025?tid=1765642038336",    
    },

    {
        "name": "What price will Bitcoin hit in 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=58&type=multi",
        "polymarket_url": "https://polymarket.com/event/what-price-will-bitcoin-hit-in-2025?tid=1765642089946",    
    },

    {
        "name": "Oscars 2026: Best Supporting Actor Winner",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=140&type=multi",
        "polymarket_url": "https://polymarket.com/event/oscars-2026-best-supporting-actor-winner?tid=1765642145878",    
    },

    {
        "name": "Oscars 2026: Best Picture Winner",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=133&type=multi",
        "polymarket_url": "https://polymarket.com/event/oscars-2026-best-picture-winner?tid=1765642213617",    
    },

    {
        "name": "Bank of Japan decision in January?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=120&type=multi",
        "polymarket_url": "https://polymarket.com/event/bank-of-japan-decision-in-december-425?tid=1765642410195",    
    },

    {
        "name": "Oscars 2026: Best Actress Winner",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=139&type=multi",
        "polymarket_url": "https://polymarket.com/event/oscars-2026-best-actress-winner?tid=1765642599486",    
    },

    {
        "name": "What will NVIDIA (NVDA) close at in 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=123&type=multi",
        "polymarket_url": "https://polymarket.com/event/what-will-nvidia-nvda-close-at-in-2025?tid=1765642686193",    
    },

    {
        "name": "Oscars 2026: Best Actor Winner",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=137&type=multi",
        "polymarket_url": "https://polymarket.com/event/oscars-2026-best-actor-winner?tid=1765642757916",    
    },

    {
        "name": "US Fed Rate Decision in March?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=75&type=multi",
        "polymarket_url": "https://polymarket.com/event/fed-decision-in-march-885?tid=1765642802095",    
    },

    {
        "name": "Who will Trump nominate as Fed Chair?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=107&type=multi",
        "polymarket_url": "https://polymarket.com/event/who-will-trump-nominate-as-fed-chair?tid=1765642879988",    
    },

    {
        "name": "What price will Solana hit in 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=85&type=multi",
        "polymarket_url": "https://polymarket.com/event/what-price-will-solana-hit-in-december-233?tid=1765642929390",    
    },

    {
        "name": "What price will Solana hit before 2026?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=85&type=multi",
        "polymarket_url": "https://polymarket.com/event/what-price-will-solana-hit-before-2026",    
    },

    {
        "name": "Oscars 2026: Best Supporting Actress Winner",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=141&type=multi",
        "polymarket_url": "https://polymarket.com/event/oscars-2026-best-supporting-actress-winner?tid=1765643103137",    
    },

    {
        "name": "US x Venezuela military engagement by...?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=144&type=multi",
        "polymarket_url": "https://polymarket.com/event/us-x-venezuela-military-engagement-by?tid=1765643355077",    
    },

    {
        "name": "What will Apple (AAPL) close at in 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=122&type=multi",
        "polymarket_url": "https://polymarket.com/event/what-will-apple-aapl-close-at-in-2025?tid=1765643499189",    
    },

    {
        "name": "Super Bowl Champion 2026",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=81&type=multi",
        "polymarket_url": "https://polymarket.com/event/super-bowl-champion-2026-731?tid=1765643570388",    
    },

    {
        "name": "What will Microsoft (MSFT) close at in 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=126&type=multi",
        "polymarket_url": "https://polymarket.com/event/what-will-microsoft-msft-close-at-in-2025?tid=1765643614579",    
    },

    {
        "name": "Largest Company end of 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=65&type=multi",
        "polymarket_url": "https://polymarket.com/event/largest-company-end-of-2025?tid=1765643649433",    
    },

    {
        "name": "Which company has best AI model end of 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=66&type=multi",
        "polymarket_url": "https://polymarket.com/event/which-company-has-best-ai-model-end-of-2025?tid=1765643691071",    
    },

    {
        "name": "Russia x Ukraine ceasefire in 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=1278",
        "polymarket_url": "https://polymarket.com/event/russia-x-ukraine-ceasefire-in-2025?tid=1765643740433",    
    },

    {
        "name": "Will the U.S. invade Venezuela by...?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=128&type=multi",
        "polymarket_url": "https://polymarket.com/event/will-the-us-invade-venezuela-in-2025?tid=1765643828105",    
    },

    {
        "name": "TikTok sale announced by...?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=160&type=multi",
        "polymarket_url": "https://polymarket.com/event/tiktok-sale-announced-in-2025?tid=1765643877931",    
    },

    {
        "name": "Russia x Ukraine ceasefire by ...?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=127&type=multi",
        "polymarket_url": "https://polymarket.com/event/russia-x-ukraine-ceasefire-by-january-31-2026?tid=1765643961152",    
    },

    {
        "name": "Maduro out by...?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=116&type=multi",
        "polymarket_url": "https://polymarket.com/event/maduro-out-in-2025?tid=1765644008281",    
    },

    {
        "name": "Maduro out by...?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=116&type=multi",
        "polymarket_url": "https://polymarket.com/event/maduro-out-in-2025?tid=1765644008281",    
    },

    {
        "name": "Highest grossing movie in 2025?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=84&type=multi",
        "polymarket_url": "https://polymarket.com/event/highest-grossing-movie-in-2025?tid=1765644316875",    
    },

    {
        "name": "Boxing: Jake Paul vs. Anthony Joshua",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=165&type=multi", 
        "polymarket_url": "https://polymarket.com/event/boxing-jake-paul-vs-anthony-joshua-third-option-included?tid=1765818628889", 
    },


    {
        "name": "OpenAI IPO by...?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=171&type=multi", 
        "polymarket_url": "https://polymarket.com/event/openai-ipo-by?tid=1765907490700", 
    },

    {
        "name": "Will Theo launch a token by...?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=172&type=multi", 
        "polymarket_url": "https://polymarket.com/event/will-theo-launch-a-token-by?tid=1765907536750", 
    },

    {
        "name": "Pump.fun airdrop by ....?",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=174&type=multi", 
        "polymarket_url": "https://polymarket.com/event/pumpfun-airdop-by?tid=1765907596260", 
    },

    {
        "name": "Bitcoin above ... on December 17?(By 12:00 ET)",
        "type": "categorical",  # 多项
        "opinion_url": "https://app.opinion.trade/detail?topicId=170&type=multi", 
        "polymarket_url": "https://polymarket.com/event/bitcoin-above-on-december-17?tid=1765907422792", 
    },

    # 例子：BTC 单一二元市场
    {
        "name": "Bitcoin Up or Down on December 17?(By 12:00 ET)",
        "type": "binary",
        "opinion_url": "https://app.opinion.trade/detail?topicId=2582",
        "polymarket_url": "https://polymarket.com/event/bitcoin-up-or-down-on-december-17",
    },

    {
        "name": "TikTok sale announced by Dec 31?",
        "type": "binary",
        "opinion_url": "https://app.opinion.trade/detail?topicId=1284",
        "polymarket_url": "https://polymarket.com/event/tiktok-sale-announced-in-2025?tid=1765600930291",
    },

    {
        "name": "Another critical Cloudflare incident by December 31?",
        "type": "binary",
        "opinion_url": "https://app.opinion.trade/detail?topicId=2362",
        "polymarket_url": "https://polymarket.com/event/another-cloudflare-outage-by-december-31?tid=1765642362571",
    },

    {
        "name": "Supreme Court rules in favor of Trump's tariffs?",
        "type": "binary",
        "opinion_url": "https://app.opinion.trade/detail?topicId=1546",
        "polymarket_url": "https://polymarket.com/event/will-the-supreme-court-rule-in-favor-of-trumps-tariffs",
    },
]

# ================== main ==================

if __name__ == "__main__":
    print("=== 根据 URL 自动生成 market_token_pairs.json（支持缓存增量更新）===\n")

    if not URL_PAIRS_FOR_DEBUG:
        print("URL_PAIRS_FOR_DEBUG 为空，请先填入市场链接。")
        raise SystemExit(1)

    out_path = os.path.join(os.path.dirname(__file__), "market_token_pairs.json")

    # ✅ 强制全量刷新：PowerShell 里先 set FORCE_REFRESH=1
    refresh = os.getenv("FORCE_REFRESH", "").strip() == "1"

    try:
        results = build_all(
            URL_PAIRS_FOR_DEBUG,
            cache_path=out_path,   # 👈 直接用上一版输出当缓存
            refresh=refresh,
            keep_cache_on_error=True,
        )
    except TokenFetcherError as e:
        print("❌ 生成失败：", e)
        raise SystemExit(1)
    except Exception as e:
        print("❌ 未知错误：", e)
        raise SystemExit(1)

    write_market_token_pairs_json(results, out_path)
    print(f"=== 已写入 {out_path} ===")
    if refresh:
        print("（本次为 FORCE_REFRESH=1，全量刷新模式）")
