"""End-to-end smoke test against real yfinance, using a throwaway database.

    .venv\\Scripts\\python scripts\\smoke.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["STOCKSIM_DB"] = str(Path(tempfile.mkdtemp()) / "smoke.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def show(label, r):
    body = r.json()
    text = json.dumps(body, ensure_ascii=False)
    print(f"[{r.status_code}] {label}: {text[:300]}{'…' if len(text) > 300 else ''}")
    return body


with TestClient(app) as c:
    show("status (live)", c.get("/api/market/status"))
    show("search apple", c.get("/api/market/search", params={"q": "apple"}))
    show("quote AAPL", c.get("/api/market/quote/AAPL"))
    show("quote bad", c.get("/api/market/quote/ZZZZNOTREAL"))
    print("1d bars:", len(c.get("/api/market/bars/AAPL", params={"range": "1d"}).json()))
    print("1y bars:", len(c.get("/api/market/bars/AAPL", params={"range": "1y"}).json()))
    show("live market buy", c.post("/api/orders", json={"symbol": "AAPL", "side": "buy", "type": "market", "qty": 10}))
    lim = show("live limit buy", c.post("/api/orders", json={"symbol": "AAPL", "side": "buy", "type": "limit",
                                                             "qty": 10, "limit_price": 100}))
    show("portfolio", c.get("/api/portfolio"))
    show("cancel", c.post(f"/api/orders/{lim['id']}/cancel"))

    g = show("new replay", c.post("/api/replay/new", json={"start_date": "2020-03-02"}))
    aid = g["id"]
    show("replay status", c.get("/api/market/status", params={"account_id": aid}))
    show("replay buy NVDA", c.post("/api/orders", json={"account_id": aid, "symbol": "NVDA", "side": "buy",
                                                        "type": "market", "qty": 100}))
    show("replay limit", c.post("/api/orders", json={"account_id": aid, "symbol": "SPY", "side": "buy",
                                                     "type": "limit", "qty": 10, "limit_price": 250}))
    show("advance month", c.post("/api/replay/advance", json={"account_id": aid, "step": "month"}))
    show("replay portfolio", c.get("/api/portfolio", params={"account_id": aid}))
    bars = c.get("/api/market/bars/NVDA", params={"account_id": aid}).json()
    print("replay bars last:", bars[-1])
    show("orders", c.get("/api/orders", params={"account_id": aid}))
