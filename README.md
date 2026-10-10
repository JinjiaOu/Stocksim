# StockSim

本地美股模拟交易小游戏。真实行情（yfinance），虚拟资金。

## 玩法

双击 `start.bat`：首次运行会自动创建 Python 环境并安装依赖，然后打开浏览器（http://127.0.0.1:8765/）。关掉黑色窗口即退出。

- **实时账户**：美东 09:30–16:00 交易时段内用最新行情交易。市价单立即成交；限价单在程序运行期间每 30 秒检查一次，当日有效。
- **历史回放**：点"新回放"，从过去某天开局（可随机），用"半天 / 1 天 / 1 周 / 1 月"推进时间，最后和同期持有 SPY 比收益。收市时也能玩。

第一次玩股票？点顶部的"新手指南"（[web/guide.html](web/guide.html)）。

存档在 `data/stocksim.db`，删除即全部重来。

## 开发

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python scripts\smoke.py   # 真实行情端到端检查（使用临时数据库）
```

设计与规则见 [DESIGN.md](DESIGN.md)。行情来自 Yahoo Finance 非官方接口，仅供个人使用。
