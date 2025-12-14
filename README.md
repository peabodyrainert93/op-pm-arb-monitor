OP/PM 套利通知 TG 机器人（Opinion × Polymarket）
本仓库用于：
  维护要监控的市场 URL 列表（Opinion + Polymarket）
  自动生成 market_token_pairs.json（映射 Opinion/Polymarket 的 token）
  轮询订单簿，发现套利空间后通过 Telegram 发送提醒

目录与文件说明（四个核心文件）
  token_registry.py
    你维护市场链接的地方（URL_PAIRS_FOR_DEBUG）。也支持直接运行生成 market_token_pairs.json（用环境变量 FORCE_REFRESH 控制是否强制刷新）。
  
  token_registry_core.py
    Token 映射生成的核心逻辑（一般不直接运行）。支持从环境变量读取 OPINION_API_KEYS（多 key，逗号分隔）或 OPINION_API_KEY（单 key）。

  run_token_registry.py
    生成/刷新 market_token_pairs.json 的入口脚本（推荐用这个跑），支持命令行参数控制并发、间隔、重试、是否 refresh。

  run_arb_monitor.py
    监控 & 套利提醒入口脚本：读取 market_token_pairs.json，拉取 Opinion + Polymarket 盘口，满足阈值则 Telegram 提醒。支持 --once 单次自检与常驻轮询。
