OP/PM 套利通知 TG 机器人（Opinion × Polymarket）

本仓库用于：

维护要监控的市场 URL 列表

生成 market_token_pairs.json（映射 Opinion/Polymarket 的 token）

轮询订单簿，发现套利空间后通过 Telegram 发送提醒

文件说明（四个核心文件）

token_registry.py：你维护市场链接的地方（URL_PAIRS_FOR_DEBUG）

token_registry

token_registry_core.py：token 映射生成的核心逻辑（被 run_token_registry.py 调用）

token_registry_core

run_token_registry.py：生成/刷新 market_token_pairs.json 的入口脚本

run_token_registry

run_arb_monitor.py：监控 & 套利提醒入口脚本（读取 market_token_pairs.json）

run_arb_monitor

0) 服务器部署前准备
A. 拉代码
git clone https://github.com/<yourname>/op-pm-arb-monitor.git
cd op-pm-arb-monitor

B. 安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

1) 在服务器上填写 .env（不会上传到 GitHub）

.env 只存在于服务器本地，用来放密钥/Token，不提交到仓库。

在仓库根目录创建 .env（你也可以从 .env.example 复制）：

cp .env.example .env
nano .env


把 .env 填成类似这样（按你的真实值替换）：

OPINION_API_KEYS=KEY1,KEY2
TELEGRAM_BOT_TOKEN=123456:ABCDEF
TELEGRAM_CHAT_ID=-100xxxxxxxxxx


⚠️ 重要说明（避免你踩坑）

token_registry_core.py 支持 OPINION_API_KEYS=key1,key2（多 Key）

token_registry_core

但当前 run_arb_monitor.py 读取的是 OPINION_API_KEY（单 Key）

run_arb_monitor

所以你在服务器上运行监控脚本时，建议在 .env 里额外再加一行（任选一个 key）：

OPINION_API_KEY=KEY1


这样：registry 用多 key，monitor 也能正常跑。

2) 配置你要监控的市场（token_registry.py）

编辑 token_registry.py 的 URL_PAIRS_FOR_DEBUG，每个对象包含：

name：你自定义的市场名字

type：binary 或 categorical

opinion_url：Opinion 市场链接

polymarket_url：Polymarket event 链接

示例结构见文件内注释：

token_registry

3) 生成 / 刷新 market_token_pairs.json（run_token_registry.py）
A. 常规生成（会利用缓存增量更新）
python run_token_registry.py


生成成功会写入仓库根目录的 market_token_pairs.json。

run_token_registry

B. 强制全量刷新（忽略旧缓存）
python run_token_registry.py --refresh

C. 可选参数（按需）

--workers：并发线程数

--opinion-interval：Opinion 请求最小间隔（秒）

--gamma-interval：Gamma 请求最小间隔（秒）

--retries / --backoff：HTTP 重试与退避

run_token_registry

示例（更保守一点，防止 429/超时）：

python run_token_registry.py --refresh --workers 6 --opinion-interval 0.35 --gamma-interval 0.35 --retries 4 --backoff 0.8

4) 启动监控与提醒（run_arb_monitor.py）

run_arb_monitor.py 默认读取仓库根目录的 market_token_pairs.json，没有该文件会报错退出。

run_arb_monitor

A. 先跑一轮自检（推荐）
python run_arb_monitor.py --once

B. 长期运行（后台）
nohup python run_arb_monitor.py >> monitor.log 2>&1 &

C. 常用参数（按需调整）

--interval：轮询间隔秒（默认 3s）

run_arb_monitor

--delta-cents：触发阈值点差（美分），例如 1.8 表示 sum_cost < 0.982 才提醒

run_arb_monitor

--cooldown：同一机会最短提醒间隔（秒）

run_arb_monitor

--workers / --op-qps / --pm-qps：并发与限速

run_arb_monitor

--min-deploy-usd：可套利资金低于该值不提醒（默认 20）

run_arb_monitor

--max-days-to-expiry：距离 endDate 超过该天数不提醒（默认 60）

run_arb_monitor

示例（更稳一点、提醒更少）：

python run_arb_monitor.py --interval 5 --delta-cents 2.0 --cooldown 180 --min-deploy-usd 30 --max-days-to-expiry 60

5) 更新市场 or 更新代码后的标准流程
A. 你改了 token_registry.py（市场列表变了）
python run_token_registry.py --refresh

B. 你 git pull 更新了仓库代码
git pull
# 建议重新跑一次自检
python run_arb_monitor.py --once
# 然后重启后台进程（按你的方式 stop/start）
