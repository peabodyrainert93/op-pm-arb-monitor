# OP/PM 套利通知 TG 机器人（Opinion × Polymarket）

用于监控 Opinion 与 Polymarket 同一事件的盘口价差，发现套利空间后通过 Telegram 发送提醒。

---

## 你会用到的 4 个脚本

| 文件 | 作用 | 你通常怎么用 |
|------|------|--------------|
| `token_registry.py` | 维护你要监控的市场 URL 列表（`URL_PAIRS_FOR_DEBUG`） | **编辑配置**（一般不直接跑） |
| `token_registry_core.py` | 生成 token 映射的核心逻辑（供 registry 调用） | 一般不直接跑 |
| `run_token_registry.py` | 生成/刷新 `market_token_pairs.json` | **经常跑**：新增市场/更新映射时 |
| `run_arb_monitor.py` | 读取 `market_token_pairs.json` 并开始监控 + TG 提醒 | **常驻跑**：服务器后台运行 |

---

## 环境要求

- Python 3.9+（推荐 3.10+）
- 服务器可访问外网（Opinion / Polymarket / Telegram）

---

## 快速开始（服务器）

### 1) 拉取代码 & 安装依赖

```bash
git clone https://github.com/peabodyrainert93/op-pm-arb-monitor.git
cd op-pm-arb-monitor

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) 在服务器上创建并填写 `.env`（不会上传 GitHub）

`.env` 只存在于服务器本地，用来存放密钥与 TG 配置，不要上传到仓库。

在仓库根目录创建 `.env`：

```bash
cp .env.example .env
nano .env
```

`.env` 示例（把值换成你自己的）：

```env
OPINION_API_KEYS=KEY1,KEY2
OPINION_API_KEY=KEY1
TELEGRAM_BOT_TOKEN=123456:ABCDEF
TELEGRAM_CHAT_ID=-100xxxxxxxxxx
```

字段说明：

- `OPINION_API_KEYS`：多 key（逗号分隔），主要供 **生成映射**（`run_token_registry.py`）使用
- `OPINION_API_KEY`：单 key，供 **监控脚本**（`run_arb_monitor.py`）使用（建议填其中一个 key 即可）
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`：发送 Telegram 提醒所需

---

## 使用流程（推荐顺序）

### 第一步：配置你要监控的市场（`token_registry.py`）

打开 `token_registry.py`，维护 `URL_PAIRS_FOR_DEBUG` 列表。每个条目需要：

- `name`：你自定义的市场名称
- `type`：`binary` 或 `categorical`
- `opinion_url`：Opinion 市场链接（带 `topicId`）
- `polymarket_url`：Polymarket event 链接（slug）

保存后进入下一步生成映射文件。

### 第二步：生成 / 刷新映射文件（`run_token_registry.py`）

生成的文件：`market_token_pairs.json`  
监控脚本会读取它，所以 **必须先生成**。

常规生成（默认增量更新）：

```bash
python run_token_registry.py
```

强制全量刷新（推荐：新增市场 / 映射不对 / 想彻底重建）：

```bash
python run_token_registry.py --refresh
```

常用可选参数（按需）：

- `--workers`：线程数（默认使用 core 的配置；监控脚本默认 8）
- `--opinion-interval`：Opinion 每次请求最小间隔（秒）
- `--gamma-interval`：Polymarket Gamma 每次请求最小间隔（秒）
- `--retries`：HTTP 最大重试次数
- `--backoff`：退避基数秒（越大越保守）
- `--refresh`：忽略缓存强制重抓

示例（更快一点）：

```bash
python run_token_registry.py --refresh --workers 6 --opinion-interval 0.35 --gamma-interval 0.35 --retries 4 --backoff 0.6
```

### 第三步：启动监控与提醒（`run_arb_monitor.py`）

运行前确保仓库根目录存在 `market_token_pairs.json`。如果没有，先执行上一步生成。

先单次自检（推荐）：

```bash
python run_arb_monitor.py --once
```

常驻运行（后台）：

```bash
nohup python run_arb_monitor.py >> monitor.log 2>&1 &
```

查看日志：

```bash
tail -f monitor.log
```

停止进程（示例）：

```bash
pkill -f run_arb_monitor.py
```

---

## `run_arb_monitor.py` 常用参数

- `--json`：指定 `market_token_pairs.json` 路径（默认：仓库根目录）
- `--interval`：轮询间隔秒（默认 3）
- `--delta-cents`：套利阈值（美分，默认 1.8）
- `--cooldown`：同一条机会最短提醒间隔（秒，默认 120）
- `--once`：只跑一轮就退出（用于自检）
- `--workers`：Opinion 并发线程数（默认 8）
- `--op-qps`：Opinion 限速 QPS（默认 6）
- `--pm-qps`：Polymarket 限速 QPS（默认 3）
- `--pm-batch / --no-pm-batch`：是否使用 `/books` 批量（默认开启，推荐）
- `--min-deploy-usd`：可套利资金低于该值不提醒（默认 20）
- `--max-days-to-expiry`：距离 Polymarket `endDate` 超过该天数不提醒（默认 60）

示例（更稳一点、提醒更少）：

```bash
python run_arb_monitor.py --interval 5 --delta-cents 2.0 --cooldown 180 --min-deploy-usd 30 --max-days-to-expiry 60
```

---

## `token_registry.py`（可选：直接运行）

一般推荐用 `run_token_registry.py`。如果你想直接跑 `token_registry.py` 也可以：

默认增量：

```bash
python token_registry.py
```

强制全量刷新（Linux/macOS）：

```bash
FORCE_REFRESH=1 python token_registry.py
```

---

## 更新流程（本地 → GitHub → 服务器）

### A) 本地更新并推送到 GitHub

```bash
git status
git add .
git commit -m "your message"
git push origin main
```

### B) 服务器拉取最新代码并重启监控

```bash
cd op-pm-arb-monitor
git pull

source .venv/bin/activate
pip install -r requirements.txt
```

如果你更新了市场列表或怀疑映射过期，记得刷新：

```bash
python run_token_registry.py --refresh
```

重启监控（示例）：

```bash
pkill -f run_arb_monitor.py || true
nohup python run_arb_monitor.py >> monitor.log 2>&1 &
```

---

## 常见问题

### 1) 没有 Telegram 提醒，只在终端打印

请检查服务器 `.env` 是否配置了：

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

另外确认 Telegram Bot 已加入对应群组/频道，并且你填的 `TELEGRAM_CHAT_ID` 正确（群组一般是 `-100...` 这种格式）。

### 2) 找不到 `market_token_pairs.json`

先生成：

```bash
python run_token_registry.py --refresh
```

### 3) Opinion Key 报错 / 缺少 Key

确保 `.env` 至少包含：

```env
OPINION_API_KEY=KEY1
```

如果你打算用多 key 轮换，也可以填：

```env
OPINION_API_KEYS=KEY1,KEY2
```

---

## 安全提醒

- 不要把 `.env` 上传到 GitHub（里面有 API Key / Telegram Token）
- 如果不小心泄露过 Token / Key，请立刻在对应平台撤销并重新生成

