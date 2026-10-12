# StockSim — 设计文档（精简版 v1.1）

> 基于 v1.0（2026-10-08）精简，2026-10-09。目标：一个想玩时双击就开、关窗即停的本地美股模拟交易小游戏。真实行情（yfinance），虚拟资金。

## 1. 原则

- 单用户、Windows 本地。双击 `start.bat` → 启动单个 Python 进程 → 自动打开浏览器；关掉控制台窗口即结束，无后台任务、无 `stop.bat`。
- 存档在 SQLite，持久保存；重开后按最新可用行情重估。
- 现金账户、只做多、整股、无杠杆、美股/ETF、USD。
- 行情真实，成交是**模拟**的，UI 始终标注"虚拟交易"和报价时间。
- 正确性优先：金额以整数美分记账；不在市场关闭或行情过期时成交。

## 2. 技术栈

| 层 | 选择 |
|---|---|
| 后端 | Python 3.14 + FastAPI + Uvicorn，同进程托管静态前端，只监听 127.0.0.1 |
| 行情 | yfinance（封装在 `MarketDataProvider`） |
| 存储 | 标准库 `sqlite3`，WAL，外键开启；文件位于 `data/stocksim.db`（不进 git） |
| 前端 | 静态 HTML + 原生 JS（ES modules），无构建步骤 |
| 图表 | TradingView Lightweight Charts（CDN，固定版本） |
| 交易日历 | 首版：周一至周五 09:30–16:00 ET + 以 yfinance 最新 1m K 线时间判断是否开市（覆盖节假日）；需要时再引入 `exchange_calendars` |

## 3. yfinance 实测结论（P0，2026-10-09，yfinance 1.7.0）

- `fast_info.last_price` 可用作最新价，约 0.3–1.3s/次。
- `history(period="1d", interval="1m")`：常规时段 390 根，带 ET 时区；`prepost=True` 覆盖 04:00–20:00。
- 1m 数据**单次请求最多 8 天**，超过返回空；5m 可取 60 天。
- **日线最后一行可能 close=NaN**（当日行未定稿），读取时需丢弃 NaN 行。
- `yf.download` 批量可用；`yf.Search` 可做代码搜索（结果含期货/外市场代码，需过滤 `quoteType` 为 EQUITY/ETF、交易所为美国）。
- 无效代码：打印 404 + 返回空 DataFrame，不抛异常 → 必须显式检查 empty。
- 拆股数据 `Ticker.splits` 可用（首版不处理拆股）。

## 4. 首版范围（MVP）

**做：**
- 股票搜索、关注列表、K 线（1m 当日 / 1d 一年）。
- 订单：市价单、限价单；有效期仅 DAY（收盘后未成交即过期）。
- 账户：单一存档，初始 $100,000；现金 = 总现金 − 买单预留；卖单预留持仓。
- 持仓、均价、市值、未实现/已实现盈亏、交易历史。
- 行情新鲜度、市场开关状态显示。

- **离线模式（历史回放）**：见 §5.1。

**推迟：** T+1 结算与 settled/unsettled 拆分、GTC、止损/OCO、拆股/分红账本、多存档、盘前盘后交易、做空/杠杆。

### 5.1 历史回放（离线模式）

- 独立的回放存档（`accounts.mode = 'replay'`），与实时账户互不影响；新开一局时旧回放存档归档。
- 开局日期随机（2000 年以后、至少留 1 年可玩）或指定。交易日历 = SPY 日线日期。
- 模拟时钟 = (sim_date, open|close)。报价为当日开盘价或收盘价；K 线只返回到当前时刻（开盘时当日 K 线只显示开盘价），不泄露未来。
- 推进步长：半天 / 1 天 / 1 周 / 1 月；每步撮合挂单：
  - 开盘→收盘：日内最低价 ≤ 买入限价（或最高价 ≥ 卖出限价）则**按限价**成交；随后当日 DAY 单过期。
  - 收盘→次日开盘：按开盘价检查限价单（跳空时按更优的开盘价成交）。
- 收盘时下的单属于下一交易日。
- 使用复权价格（`auto_adjust=True`），无需处理拆股分红；代价是早年价格与当年实际报价不同（例如 2020 年 NVDA 显示约 $7）。
- 日线历史首次使用时整段下载并缓存到 `daily_bars`；游戏进行中不重新下载（避免复权价格变动）。
- 到达最新数据后存档状态变为 `finished`。

## 5. 撮合规则

- 价格以整数万分之一美元（price4）存储，现金以美分存储；买入金额向上取整、卖出向下取整。
- 仅在常规时段、且报价 `as_of` 距现在 ≤ 3 分钟时撮合（交易日判断：工作日 09:30–16:00 ET，且 09:35 后 SPY 有 5 分钟内的新 K 线，以此识别休市日）；否则市价单拒绝（提示市场关闭/行情过期），限价单继续等待。
- 市价买：`price × (1 + slippage)`；市价卖：`price × (1 − slippage)`；默认 slippage 5bps，佣金 $0，可配置。
- 限价买：最新价 ≤ 限价 → 按 `min(限价, 最新价 × (1+slippage))` 成交；卖出对称。成交标记 `estimated`。
- 撮合循环：程序运行且市场开盘时，每 30s 刷新持仓+挂单+关注品种报价并检查挂单；离线期间不撮合、重启不追认。
- 异常保护：报价与上一次报价相比跳变 >30% 时，该次报价不撮合（下一次报价若稳定则恢复）。
- DAY 单归属交易日：收盘后/周末下单归属下一个工作日。已知限制：不识别交易所假日，节前夜间下的单会在假日后直接过期。
- 买入前校验可用现金，卖出前校验可用持仓；拒绝负现金/负持仓。

### 5.2 保证金账户（做空 / 杠杆）

- 账户类型 `cash`（默认，只做多、全额付款）或 `margin`；实时和回放都可新建保证金存档，杠杆 1x / 2x / 4x。
- 持仓数量带符号（负数 = 空头），保证金账户现金可为负（借款）。净资产 = 现金 + Σ 数量 × 价格。
- 初始保证金 = 1 / 杠杆；维持保证金：多头为初始的一半，空头为初始的 60%（2x 即 Reg T 50% / 25% / 30%）。
- 开新敞口时：所需保证金（新敞口 − 原敞口，按初始比例）+ 费用 ≤ 净资产 − 初始保证金要求 − 挂单冻结；减仓单不检查。需要所有持仓的价格，否则拒绝开新仓。限价单冻结所需保证金。
- 强制平仓：净资产 < 维持保证金时撤销全部挂单，按未实现盈亏从差到好逐个市价平仓，直到恢复。只平有可交易报价的仓位。平仓后净资产 ≤ 0 → 账户状态 `bust`（爆仓）。
  - 实时：后台每 30 秒检查；回放：每个开盘/收盘时刻检查（不看盘中高低点）。
- 融资成本按自然日计：借款（负现金）年息 8%，空头市值年费 3%，按 360 天计息。实时每天首次检查时计，回放在每次隔夜推进时计。
- 简化：任何股票都可融券，无提前召回；不模拟盘中强平。

## 6. 数据表

- `account`: id, cash_cents, reserved_cents, initial_cents, created_at
- `orders`: id, symbol, side, type, qty, limit_cents, status(open/filled/cancelled/expired/rejected), reason, reserved_cents, placed_at, trade_date, closed_at
- `fills`: id, order_id, symbol, side, qty, price_cents, fees_cents, quote_as_of, executed_at, estimated
- `positions`: symbol, qty, reserved_qty, cost_cents（总成本）, realized_cents
- `watchlist`: symbol, added_at
- `cash_ledger`: id, amount_cents, category, fill_id, created_at

订单成交时，`fills`、`positions`、`account`、`cash_ledger` 在同一事务内写入。所有时间存 UTC ISO 字符串，展示转 `America/New_York`。

## 7. 目录

```text
stocksim/
├── app/
│   ├── main.py          # FastAPI 入口 + 静态文件 + 后台撮合循环
│   ├── db.py            # sqlite 连接与建表
│   ├── market.py        # MarketDataProvider + 缓存 + 市场时段
│   ├── trading.py       # 下单、撤单、撮合、记账
│   └── api.py           # 路由
├── web/                 # index.html, app.js, style.css
├── tests/
├── scripts/p0_check.py
├── data/                # stocksim.db（gitignore）
├── requirements.txt
└── start.bat
```

## 8. API

- `GET /api/market/status`
- `GET /api/market/search?q=`
- `GET /api/market/quote/{symbol}`
- `GET /api/market/bars/{symbol}?range=1d|1y`
- `GET /api/portfolio`（现金、持仓、估值、盈亏）
- `GET /api/orders?status=`、`POST /api/orders`、`POST /api/orders/{id}/cancel`
- `GET /api/fills`
- `GET/POST/DELETE /api/watchlist`
- `GET /api/accounts`、`POST /api/account/reset`
- `POST /api/replay/new`（start_date?, initial?）、`POST /api/replay/advance`（account_id, step）

所有接口带可选 `account_id`，缺省为实时账户。

前端每 5s 轮询本地后端；后端对 yfinance 报价缓存 30s。

## 9. 验收

1. 初始 $100,000，买 100 股后现金与持仓一致（整数美分无误差）。
2. 市场关闭时市价单被拒，不按旧价成交。
3. 限价单预留资金，撤单后释放；DAY 单收盘后过期。
4. 卖出不能超过可用持仓；重复点击不会重复下单（前端提交中禁用按钮）。
5. 关闭重开后订单、成交、持仓不丢失，持仓按最新价重估。

## 10. 路线

- **P0 ✅** 行情验证（`scripts/p0_check.py`）
- **P1 ✅** 后端账本 + 撮合 + API + 历史回放 + 单元测试（`pytest`）+ 冒烟测试（`scripts/smoke.py`）
- **P2 ✅** 前端页面（仪表盘、行情/K 线、下单、持仓、历史、回放控制、SPY 基准对比）+ `start.bat`
- **P3（进行中）** ✅ 公司列表、✅ 保证金账户与做空、✅ 多存档；待定：外汇、T+1、止损、拆股分红

数据库版本记录在 `PRAGMA user_version`（当前 1）；v0→v1 自动迁移（重建 accounts / positions 表，保留数据）。

## 11. 外汇（fx）

- 只支持 EURUSD / GBPUSD / AUDUSD / NZDUSD，以美元报价，所以"数量 × 价格"直接就是美元；价格保留 4 位小数，正好是 1 点。
- 下单按 1000 单位（微型手）的整数倍；fx 账户固定是保证金账户，杠杆 10x / 20x，多空的维持保证金都是初始保证金的一半，滑点 1 bps，不计隔夜利息。
- 开盘时段是美东周日 17:00 到周五 17:00，每天 17:00 换日；开盘期间 EURUSD 超过 10 分钟没有新报价时视为休市；报价超过 300 秒算过期。
- 回放用 EURUSD 的日期作为交易日历，开局不早于 2010-01-04；Yahoo 的坏数据会被清洗（开盘跳空超过 5% 时改用前收，最高/最低价限制在 K 线实体外 5% 以内）；对比基准是持有美元现金。
- 不支持：USD/JPY 和交叉盘、5 位小数报价、隔夜利差（swap）。
