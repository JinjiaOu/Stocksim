from datetime import datetime, timedelta, timezone

import pytest

from app import db, trading
from app.models import Quote
from app.trading import TradeError

NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
DAY = "2026-10-09"


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    yield c
    c.close()


@pytest.fixture
def acct(conn):
    aid = db.live_account_id(conn)
    conn.execute("UPDATE accounts SET slippage_bps = 0")
    return aid


def q(symbol, price, tradable=True, reason=None):
    return Quote(symbol, int(price * 10000), NOW, "test", tradable, reason)


def order(conn, oid):
    return conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()


def account(conn, aid):
    return trading.get_account(conn, aid)


def position(conn, aid, sym):
    return conn.execute("SELECT * FROM positions WHERE account_id = ? AND symbol = ?", (aid, sym)).fetchone()


def assert_books_balance(conn, aid):
    a = account(conn, aid)
    ledger = conn.execute("SELECT SUM(amount_cents) s FROM cash_ledger WHERE account_id = ?", (aid,)).fetchone()["s"]
    assert ledger == a["cash_cents"]
    reserved = conn.execute("SELECT COALESCE(SUM(reserved_cents), 0) s FROM orders WHERE account_id = ? AND status = 'open'",
                            (aid,)).fetchone()["s"]
    assert reserved == a["reserved_cents"]


def buy(conn, aid, sym, qty, price, otype="market", limit=None, quote=None):
    return trading.place_order(conn, aid, sym, "buy", otype, qty, int(limit * 10000) if limit else None,
                               quote if quote is not None else q(sym, price), DAY, NOW)


def sell(conn, aid, sym, qty, price, otype="market", limit=None, quote=None):
    return trading.place_order(conn, aid, sym, "sell", otype, qty, int(limit * 10000) if limit else None,
                               quote if quote is not None else q(sym, price), DAY, NOW)


def test_acceptance_buy_100_at_200(conn, acct):
    oid = buy(conn, acct, "AAPL", 100, 200)
    assert order(conn, oid)["status"] == "filled"
    assert account(conn, acct)["cash_cents"] == 100_000_00 - 20_000_00
    p = position(conn, acct, "AAPL")
    assert (p["qty"], p["cost_cents"]) == (100, 20_000_00)
    assert_books_balance(conn, acct)


def test_slippage_and_commission(conn, acct):
    conn.execute("UPDATE accounts SET slippage_bps = 10, commission_cents = 100")
    oid = buy(conn, acct, "AAPL", 10, 100)
    assert order(conn, oid)["fill_price4"] == 100_1000  # 100 * 1.001
    assert account(conn, acct)["cash_cents"] == 100_000_00 - 1001_00 - 100
    sell(conn, acct, "AAPL", 10, 100)
    assert account(conn, acct)["cash_cents"] == 100_000_00 - 1001_00 - 100 + 999_00 - 100
    assert_books_balance(conn, acct)


def test_market_order_rejected_when_closed(conn, acct):
    oid = buy(conn, acct, "AAPL", 1, 200, quote=q("AAPL", 200, tradable=False, reason="市场未开盘"))
    o = order(conn, oid)
    assert (o["status"], o["reason"]) == ("rejected", "市场未开盘")
    assert account(conn, acct)["cash_cents"] == 100_000_00
    assert position(conn, acct, "AAPL") is None


def test_market_order_rejected_without_quote(conn, acct):
    oid = trading.place_order(conn, acct, "AAPL", "buy", "market", 1, None, None, DAY, NOW)
    assert order(conn, oid)["status"] == "rejected"


def test_insufficient_funds(conn, acct):
    oid = buy(conn, acct, "AAPL", 1000, 200)
    assert order(conn, oid)["status"] == "rejected"
    assert account(conn, acct)["cash_cents"] == 100_000_00


def test_cannot_sell_more_than_held(conn, acct):
    buy(conn, acct, "AAPL", 10, 100)
    assert order(conn, sell(conn, acct, "AAPL", 11, 100))["status"] == "rejected"
    assert order(conn, sell(conn, acct, "MSFT", 1, 100))["status"] == "rejected"


def test_realized_pnl_and_average_cost(conn, acct):
    buy(conn, acct, "AAPL", 10, 100)
    buy(conn, acct, "AAPL", 10, 200)
    sell(conn, acct, "AAPL", 5, 300)
    p = position(conn, acct, "AAPL")
    assert p["qty"] == 15
    assert p["cost_cents"] == 2250_00            # avg 150 * 15
    assert p["realized_cents"] == 750_00         # 5 * (300 - 150)
    sell(conn, acct, "AAPL", 15, 100)
    p = position(conn, acct, "AAPL")
    assert (p["qty"], p["cost_cents"], p["realized_cents"]) == (0, 0, 750_00 - 750_00)
    assert_books_balance(conn, acct)


def test_limit_buy_reserves_then_cancel_releases(conn, acct):
    oid = buy(conn, acct, "AAPL", 100, 210, otype="limit", limit=200)
    o = order(conn, oid)
    assert o["status"] == "open" and o["reserved_cents"] == 20_000_00
    a = account(conn, acct)
    assert (a["cash_cents"], a["reserved_cents"]) == (100_000_00, 20_000_00)
    # reserved cash cannot be spent twice
    assert order(conn, buy(conn, acct, "MSFT", 401, 200))["status"] == "rejected"
    assert order(conn, buy(conn, acct, "MSFT", 400, 200))["status"] == "filled"  # exactly the rest
    assert account(conn, acct)["cash_cents"] == 20_000_00
    trading.cancel_order(conn, acct, oid, NOW)
    assert account(conn, acct)["reserved_cents"] == 0
    with pytest.raises(TradeError):
        trading.cancel_order(conn, acct, oid, NOW)
    assert_books_balance(conn, acct)


def test_limit_buy_fills_when_price_drops(conn, acct):
    oid = buy(conn, acct, "AAPL", 100, 210, otype="limit", limit=200)
    assert trading.match_open_orders(conn, acct, {"AAPL": q("AAPL", 205)}, NOW) == 0
    assert trading.match_open_orders(conn, acct, {"AAPL": q("AAPL", 195, tradable=False)}, NOW) == 0
    assert trading.match_open_orders(conn, acct, {"AAPL": q("AAPL", 195)}, NOW) == 1
    o = order(conn, oid)
    assert (o["status"], o["fill_price4"]) == ("filled", 195_0000)
    a = account(conn, acct)
    assert (a["cash_cents"], a["reserved_cents"]) == (100_000_00 - 19_500_00, 0)
    assert_books_balance(conn, acct)


def test_marketable_limit_fills_immediately_at_quote(conn, acct):
    oid = buy(conn, acct, "AAPL", 10, 190, otype="limit", limit=200)
    assert order(conn, oid)["fill_price4"] == 190_0000


def test_limit_sell_reserves_shares(conn, acct):
    buy(conn, acct, "AAPL", 10, 100)
    oid = sell(conn, acct, "AAPL", 10, 100, otype="limit", limit=120)
    assert position(conn, acct, "AAPL")["reserved_qty"] == 10
    assert order(conn, sell(conn, acct, "AAPL", 1, 100))["status"] == "rejected"
    trading.match_open_orders(conn, acct, {"AAPL": q("AAPL", 125)}, NOW)
    assert order(conn, oid)["fill_price4"] == 125_0000
    p = position(conn, acct, "AAPL")
    assert (p["qty"], p["reserved_qty"], p["realized_cents"]) == (0, 0, 250_00)
    assert_books_balance(conn, acct)


def test_day_orders_expire(conn, acct):
    oid = buy(conn, acct, "AAPL", 1, 210, otype="limit", limit=200)
    assert trading.expire_orders(conn, acct, DAY, NOW) == 0
    assert trading.expire_orders(conn, acct, "2026-10-12", NOW) == 1
    assert order(conn, oid)["status"] == "expired"
    assert account(conn, acct)["reserved_cents"] == 0


def test_invalid_input_raises(conn, acct):
    with pytest.raises(TradeError):
        trading.place_order(conn, acct, "AAPL", "buy", "market", 0, None, q("AAPL", 1), DAY, NOW)
    with pytest.raises(TradeError):
        trading.place_order(conn, acct, "AAPL", "buy", "limit", 1, None, q("AAPL", 1), DAY, NOW)


def test_failed_fill_rolls_back(conn, acct):
    """A DB constraint failure mid-order must leave no partial writes."""
    conn.execute("UPDATE accounts SET commission_cents = 100")
    buy(conn, acct, "PENNY", 1, 0.01)  # 1 cent stock + $1 fee
    before = account(conn, acct)["cash_cents"]
    conn.execute("UPDATE accounts SET cash_cents = 50 WHERE id = ?", (acct,))
    conn.execute("INSERT INTO cash_ledger (account_id, amount_cents, balance_cents, category, created_at)"
                 " VALUES (?, ?, 50, 'adjust', 'x')", (acct, 50 - before))
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):  # proceeds 1c - fee 100c would make cash negative
        sell(conn, acct, "PENNY", 1, 0.01)
    assert position(conn, acct, "PENNY")["qty"] == 1
    assert conn.execute("SELECT COUNT(*) n FROM orders WHERE symbol = 'PENNY' AND side = 'sell'").fetchone()["n"] == 0
    assert_books_balance(conn, acct)


def test_portfolio_valuation(conn, acct):
    buy(conn, acct, "AAPL", 10, 100)
    pf = trading.portfolio(conn, acct, {"AAPL": q("AAPL", 110)})
    assert pf["equity"] == 100_100
    assert pf["positions"][0]["unrealized"] == 100
    assert pf["total_return"] == pytest.approx(0.001)
    pf = trading.portfolio(conn, acct, {})
    assert pf["unpriced_symbols"] == ["AAPL"]


def test_reset(conn, acct):
    buy(conn, acct, "AAPL", 10, 100)
    trading.reset_account(conn, acct, NOW + timedelta(minutes=1))
    assert account(conn, acct)["cash_cents"] == 100_000_00
    assert trading.held_symbols(conn, acct) == []
    assert_books_balance(conn, acct)
