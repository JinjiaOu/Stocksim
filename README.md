# StockSim

本地美股模拟交易小游戏。真实行情（yfinance），虚拟资金。

## 玩法

两种启动方式，任选其一，都会打开浏览器（http://127.0.0.1:8765/）：

- **exe（无需安装 Python）**：双击 `StockSim.exe`，不会弹出终端窗口。点页面右上角"退出程序"退出；直接关掉浏览器页面，约 3 分钟后程序也会自动退出。存档和日志（`data/`、`stocksim.log`）生成在 exe 所在文件夹，建议把 exe 单独放进一个文件夹使用。
- **源码**：双击 `start.bat`，首次运行会自动创建 Python 环境并安装依赖。关掉黑色窗口即退出。

需要联网（行情来自 yfinance，图表库从 CDN 加载）。

- **实时账户**：美东 09:30–16:00 交易时段内用最新行情交易。市价单立即成交；限价单在程序运行期间每 30 秒检查一次，当日有效。
- **历史回放**：点"新回放"，从过去某天开局（可随机），用"半天 / 1 天 / 1 周 / 1 月"推进时间，最后和同期持有 SPY 比收益。收市时也能玩。

第一次玩股票？点顶部的"新手指南"（[web/guide.html](web/guide.html)）。

存档在 `data/stocksim.db`（exe 版在 exe 旁边的 `data/`），删除即全部重来。

列表里的公司是手工整理的约 245 家常见公司和 ETF（[web/companies.js](web/companies.js)），不在其中的可用搜索框按代码或名称查。

## 开发

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python scripts\smoke.py   # 真实行情端到端检查（使用临时数据库）
```

## 打包成 exe

```bash
build.bat   # 需要先有 .venv；输出 dist\StockSim.exe（单文件，无控制台窗口）
```

图标在 `assets/`。若资源管理器里图标没更新，是 Windows 图标缓存，换个文件名或清缓存即可。

设计与规则见 [DESIGN.md](DESIGN.md)。行情来自 Yahoo Finance 非官方接口，仅供个人使用。
