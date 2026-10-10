import sqlite3
from datetime import datetime, timezone

import pytest

from app import db, replay, trading
from app.models import Quote, iso

NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
DAY = "2026-10-09"


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    yield c
    c.close()


def margin_account(conn, leverage=2, initial=100_000_00):
    aid = trading.create_account(conn, "live", "m", initial, NOW, account_type="margin", leverage=leverage,
                                 accrued_through="2026-10-01")
    conn.execute("UPDATE accounts SET slippage_bps = 0 WHERE id = ?", (aid,))
    return aid


def q(symbol, price, tradable=True):
    return Quote(symbol, round(price * 10000), NOW, "test", tradable, None if tradable else "closed")


def order(conn, aid, side, symbol, qty, price, otype="market", limit=None, marks=None, tradable=True):
    quote = q(symbol, price, tradable)
    marks = {**(marks or {}), symbol: quote}
    for s in trading.held_symbols(conn, aid):
        marks.setdefault(s, q(s, price))
    oid = trading.place_order(conn, aid, symbol, side, otype, qty, round(limit * 10000) if limit else None,
                              quote, DAY, NOW, marks)
    return conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()


def acct(conn, aid):
    return trading.get_account(conn, aid)


def pos(conn, aid, sym):
    return conn.execute("SELECT * FROM positions WHERE account_id = ? AND symbol = ?", (aid, sym)).fetchone()


def assert_books_balance(conn, aid):
    a = acct(conn, aid)
    ledger = conn.execute("SELECT SUM(amount_cents) s FROM cash_ledger WHERE account_id = ?", (aid,)).fetchone()["s"]
    assert ledger == a["cash_cents"]
    reserved = conn.execute("SELECT COALESCE(SUM(reserved_cents), 0) s FROM orders WHERE account_id = ? AND status = 'open'",
                            (aid,)).fetchone()["s"]
    assert reserved == a["reserved_cents"]


def test_margin_params_match_reg_t_at_2x(conn):
    a = acct(conn, margin_account(conn))
    assert (a["initial_margin_bps"], a["maint_long_bps"], a["maint_short_bps"]) == (5000, 2500, 3000)


def test_cash_account_cannot_short(conn):
    aid = db.live_account_id(conn)
    o = order(conn, aid, "sell", "AAPL", 10, 100)
    assert o["status"] == "rejected" and "不能卖空" in o["reason"]


def test_short_then_cover_for_profit(conn):
    aid = margin_account(conn)
    assert order(conn, aid, "sell", "XYZ", 100, 100)["status"] == "filled"
    assert acct(conn, aid)["cash_cents"] == 110_000_00          # short proceeds credited
    p = pos(conn, aid, "XYZ")
    assert (p["qty"], p["cost_cents"]) == (-100, 10_000_00)
    pf = trading.portfolio(conn, aid, {"XYZ": q("XYZ", 90)})
    assert pf["equity"] == 101_000 and pf["positions"][0]["unrealized"] == 1_000
    assert order(conn, aid, "buy", "XYZ", 100, 80)["status"] == "filled"
    p = pos(conn, aid, "XYZ")
    assert (p["qty"], p["cost_cents"], p["realized_cents"]) == (0, 0, 2_000_00)
    assert acct(conn, aid)["cash_cents"] == 102_000_00
    assert_books_balance(conn, aid)


def test_short_loss(conn):
    aid = margin_account(conn)
    order(conn, aid, "sell", "XYZ", 100, 100)
    order(conn, aid, "buy", "XYZ", 100, 120)
    assert pos(conn, aid, "XYZ")["realized_cents"] == -2_000_00
    assert acct(conn, aid)["cash_cents"] == 98_000_00


def test_leverage_allows_borrowing_up_to_initial_margin(conn):
    aid = margin_account(conn)
    assert order(conn, aid, "buy", "XYZ", 2000, 100)["status"] == "filled"   # $200k on $100k equity
    assert acct(conn, aid)["cash_cents"] == -100_000_00
    o = order(conn, aid, "buy", "XYZ", 1, 100)
    assert o["status"] == "rejected" and "保证金不足" in o["reason"]
    # reducing exposure never needs margin
    assert order(conn, aid, "sell", "XYZ", 100, 100)["status"] == "filled"
    assert_books_balance(conn, aid)


def test_one_x_margin_account_can_short_but_not_borrow(conn):
    aid = margin_account(conn, leverage=1)
    assert order(conn, aid, "buy", "XYZ", 1001, 100)["status"] == "rejected"
    assert order(conn, aid, "sell", "ABC", 1000, 100)["status"] == "filled"


def test_flip_long_to_short(conn):
    aid = margin_account(conn)
    order(conn, aid, "buy", "XYZ", 10, 100)
    order(conn, aid, "sell", "XYZ", 15, 110)
    p = pos(conn, aid, "XYZ")
    assert (p["qty"], p["cost_cents"], p["realized_cents"]) == (-5, 550_00, 100_00)
    order(conn, aid, "buy", "XYZ", 5, 100)
    p = pos(conn, aid, "XYZ")
    assert (p["qty"], p["realized_cents"]) == (0, 100_00 + 50_00)
    assert_books_balance(conn, aid)


def test_margin_limit_order_reserves_margin(conn):
    aid = margin_account(conn)
    o = order(conn, aid, "sell", "XYZ", 100, 100, otype="limit", limit=110)
    assert o["status"] == "open" and o["reserved_cents"] == 5_500_00
    pf = trading.portfolio(conn, aid, {})
    assert pf["margin"]["excess"] == 100_000 - 5_500
    trading.match_open_orders(conn, aid, {"XYZ": q("XYZ", 111)}, NOW)
    assert acct(conn, aid)["reserved_cents"] == 0
    assert pos(conn, aid, "XYZ")["qty"] == -100
    assert_books_balance(conn, aid)


def test_rejects_when_a_holding_cannot_be_priced(conn):
    aid = margin_account(conn)
    order(conn, aid, "buy", "XYZ", 10, 100)
    oid = trading.place_order(conn, aid, "ABC", "buy", "market", 1, None, q("ABC", 10), DAY, NOW, {"ABC": q("ABC", 10)})
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    assert o["status"] == "rejected" and "XYZ" in o["reason"]


def test_maintenance_call_liquidates(conn):
    aid = margin_account(conn)
    order(conn, aid, "buy", "XYZ", 2000, 100)
    order(conn, aid, "sell", "XYZ", 1, 100, otype="limit", limit=150)   # open order gets cancelled
    assert trading.enforce_maintenance(conn, aid, {"XYZ": q("XYZ", 70)}, NOW) is None  # 40k equity >= 35k
    pf = trading.portfolio(conn, aid, {"XYZ": q("XYZ", 70)})
    assert pf["positions"][0]["liquidation_price"] == pytest.approx(66.6667, abs=1e-3)
    res = trading.enforce_maintenance(conn, aid, {"XYZ": q("XYZ", 66)}, NOW)
    assert res["closed"] == ["XYZ"] and res["cancelled"] == 1 and not res["bust"]
    assert trading.held_symbols(conn, aid) == []
    a = acct(conn, aid)
    assert (a["cash_cents"], a["reserved_cents"], a["status"]) == (32_000_00, 0, "active")
    reasons = [r["reason"] for r in conn.execute("SELECT reason FROM orders WHERE account_id = ?", (aid,))]
    assert "强制平仓：低于维持保证金" in reasons and "保证金不足，系统撤单" in reasons
    assert_books_balance(conn, aid)


def test_liquidation_waits_for_tradable_quote(conn):
    aid = margin_account(conn)
    order(conn, aid, "buy", "XYZ", 2000, 100)
    assert trading.enforce_maintenance(conn, aid, {"XYZ": q("XYZ", 60, tradable=False)}, NOW)["closed"] == []
    assert trading.held_symbols(conn, aid) == ["XYZ"]
    assert trading.portfolio(conn, aid, {"XYZ": q("XYZ", 60)})["margin"]["margin_call"]


def test_gap_through_zero_is_bust(conn):
    aid = margin_account(conn)
    order(conn, aid, "buy", "XYZ", 2000, 100)
    res = trading.enforce_maintenance(conn, aid, {"XYZ": q("XYZ", 45)}, NOW)
    assert res["bust"]
    a = acct(conn, aid)
    assert a["status"] == "bust" and a["cash_cents"] == -10_000_00
    with pytest.raises(trading.TradeError):
        order(conn, aid, "buy", "XYZ", 1, 45)


def test_short_squeeze_liquidation(conn):
    aid = margin_account(conn)
    order(conn, aid, "sell", "XYZ", 2000, 50)      # $100k short, cash $200k
    # equity = 200k - 2000p; maint = 0.3 * 2000p -> call above ~76.92
    assert trading.enforce_maintenance(conn, aid, {"XYZ": q("XYZ", 75)}, NOW) is None
    res = trading.enforce_maintenance(conn, aid, {"XYZ": q("XYZ", 78)}, NOW)
    assert res["closed"] == ["XYZ"]
    assert acct(conn, aid)["cash_cents"] == 200_000_00 - 156_000_00


def test_financing_accrual(conn):
    aid = margin_account(conn)
    assert order(conn, aid, "buy", "XYZ", 1800, 100)["status"] == "filled"   # cash -> -80k
    assert order(conn, aid, "sell", "ABC", 100, 100)["status"] == "filled"   # $10k short, cash -> -70k
    marks = {"XYZ": q("XYZ", 100), "ABC": q("ABC", 100)}
    charged = trading.accrue_financing(conn, aid, "2026-10-11", marks, NOW)  # 10 days from 10-01
    interest = -(-70_000_00 * 800 * 10 // (10000 * 360))
    borrow = -(-10_000_00 * 300 * 10 // (10000 * 360))
    assert charged == interest + borrow
    assert trading.accrue_financing(conn, aid, "2026-10-11", marks, NOW) == 0     # idempotent per day
    assert acct(conn, aid)["accrued_through"] == "2026-10-11"
    cats = {r["category"] for r in conn.execute("SELECT category FROM cash_ledger WHERE account_id = ?", (aid,))}
    assert {"margin_interest", "borrow_fee"} <= cats
    assert_books_balance(conn, aid)


def test_cash_account_never_accrues(conn):
    aid = db.live_account_id(conn)
    assert trading.accrue_financing(conn, aid, "2030-01-01", {}, NOW) == 0


# --- replay ---------------------------------------------------------------------

SPY = [("2020-01-02", 100, 102, 99, 101), ("2020-01-03", 101, 103, 100, 102),
       ("2020-01-06", 102, 104, 101, 103), ("2020-01-07", 103, 105, 102, 104)]
XYZ = [("2020-01-02", 40, 41, 39, 40), ("2020-01-03", 40, 41, 39, 41),
       ("2020-01-06", 41, 45, 40, 44), ("2020-01-07", 44, 60, 44, 59)]


@pytest.fixture
def rconn(conn):
    for sym, rows in (("SPY", SPY), ("XYZ", XYZ)):
        conn.executemany("INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, 1000)",
                         [(sym, d, o * 10000, h * 10000, lo * 10000, c * 10000) for d, o, h, lo, c in rows])
        conn.execute("INSERT INTO bar_meta VALUES (?, ?, ?, ?)", (sym, rows[0][0], rows[-1][0], iso(NOW)))
    return conn


def test_replay_short_squeeze_goes_bust(rconn):
    aid = replay.new_game(rconn, None, 10_000_00, "2020-01-02", account_type="margin", leverage=4)
    rconn.execute("UPDATE accounts SET slippage_bps = 0 WHERE id = ?", (aid,))
    a = acct(rconn, aid)
    oid = trading.place_order(rconn, aid, "XYZ", "sell", "market", 900, None, replay.quote(rconn, a, "XYZ"),
                              replay.session_date(rconn, a), replay.now_of(a), {"XYZ": replay.mark(rconn, a, "XYZ")})
    assert rconn.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()["status"] == "filled"
    ev = replay.advance(rconn, aid, 10)
    assert ev["bust"] and ev["finished"] and ev["liquidated"] == 1
    assert ev["financing"] > 0                         # borrow fees accrued overnight
    assert (ev["sim_date"], ev["sim_phase"]) == ("2020-01-07", "close")
    assert acct(rconn, aid)["status"] == "bust"


# --- migration ------------------------------------------------------------------

V0_SCHEMA = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY, mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  name TEXT NOT NULL, initial_cents INTEGER NOT NULL, cash_cents INTEGER NOT NULL, reserved_cents INTEGER NOT NULL DEFAULT 0,
  slippage_bps INTEGER NOT NULL DEFAULT 5, commission_cents INTEGER NOT NULL DEFAULT 0, start_date TEXT, sim_date TEXT,
  sim_phase TEXT, created_at TEXT NOT NULL, CHECK (cash_cents >= 0));
CREATE TABLE positions (account_id INTEGER NOT NULL REFERENCES accounts(id), symbol TEXT NOT NULL,
  qty INTEGER NOT NULL CHECK (qty >= 0), reserved_qty INTEGER NOT NULL DEFAULT 0, cost_cents INTEGER NOT NULL DEFAULT 0,
  realized_cents INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (account_id, symbol));
CREATE TABLE orders (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id), symbol TEXT NOT NULL,
  side TEXT NOT NULL, type TEXT NOT NULL, qty INTEGER NOT NULL, limit_price4 INTEGER, status TEXT NOT NULL, reason TEXT,
  reserved_cents INTEGER NOT NULL DEFAULT 0, fill_price4 INTEGER, trade_date TEXT NOT NULL, placed_at TEXT NOT NULL,
  closed_at TEXT);
INSERT INTO accounts (id, mode, name, initial_cents, cash_cents, created_at) VALUES (1, 'live', '实时账户', 10000000, 9000000, 'x');
INSERT INTO positions VALUES (1, 'AAPL', 50, 0, 1000000, 0);
INSERT INTO orders VALUES (1, 1, 'AAPL', 'buy', 'market', 50, NULL, 'filled', NULL, 0, 2000000, '2026-10-09', 'x', 'x');
"""


def test_migrates_v0_database(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(V0_SCHEMA)
    old.close()
    c = db.connect(path)
    db.init(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    a = trading.get_account(c, 1)
    assert (a["account_type"], a["cash_cents"], a["initial_margin_bps"]) == ("cash", 9_000_000, 10000)
    assert pos(c, 1, "AAPL")["qty"] == 50
    assert c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 1
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    # new constraints are in force: cash accounts still cannot go negative
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE accounts SET cash_cents = -1 WHERE id = 1")
    c.close()
    c = db.connect(path)  # re-running init on a migrated DB is a no-op
    db.init(c)
    assert trading.get_account(c, 1)["cash_cents"] == 9_000_000
    c.close()
