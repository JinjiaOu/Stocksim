import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import api, db, market, replay, trading
from app.models import Quote, iso

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
DAY = "2026-10-09"


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    yield c
    c.close()


def fx_account(conn, leverage=20, initial=100_000_00, slip_zero=True):
    aid = trading.create_account(conn, "live", "fx", initial, NOW, leverage=leverage, market="fx",
                                 accrued_through="2026-10-01")
    if slip_zero:
        conn.execute("UPDATE accounts SET slippage_bps = 0 WHERE id = ?", (aid,))
    return aid


def us_account(conn, leverage=2, initial=100_000_00):
    aid = trading.create_account(conn, "live", "us", initial, NOW, account_type="margin", leverage=leverage,
                                 accrued_through="2026-10-01")
    conn.execute("UPDATE accounts SET slippage_bps = 0 WHERE id = ?", (aid,))
    return aid


def q(symbol, price, tradable=True):
    return Quote(symbol, round(price * 10000), NOW, "test", tradable, None if tradable else "closed")


def acct(conn, aid):
    return trading.get_account(conn, aid)


def place(conn, aid, symbol, side, qty, price, otype="market", limit=None):
    quote = q(symbol, price)
    marks = {symbol: quote}
    for s in trading.held_symbols(conn, aid):
        marks.setdefault(s, q(s, price))
    return trading.place_order(conn, aid, symbol, side, otype, qty,
                               round(limit * 10000) if limit else None,
                               quote, DAY, NOW, marks)


def status(conn, oid):
    return conn.execute("SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()["status"]


# --- live hours / session dates ---------------------------------------------------

def test_fx_hours_week_boundaries():
    # Friday 16:59 ET open, 17:01 ET closed; Sunday 16:59 ET closed, 17:01 ET open.
    assert market.in_fx_hours(datetime(2026, 10, 9, 16, 59, tzinfo=ET))
    assert not market.in_fx_hours(datetime(2026, 10, 9, 17, 1, tzinfo=ET))
    assert not market.in_fx_hours(datetime(2026, 10, 11, 16, 59, tzinfo=ET))
    assert market.in_fx_hours(datetime(2026, 10, 11, 17, 1, tzinfo=ET))
    # Midweek is open all day.
    assert market.in_fx_hours(datetime(2026, 10, 7, 3, 0, tzinfo=ET))
    assert not market.in_fx_hours(datetime(2026, 10, 10, 12, 0, tzinfo=ET))  # Saturday


def test_fx_session_date_rolls_at_17_et():
    def et(y, m, d, hh, mm):
        return datetime(y, m, d, hh, mm, tzinfo=ET)

    # Monday rolls to Tuesday at 17:00.
    assert market.fx_session_date(et(2026, 10, 12, 16, 59)).isoformat() == "2026-10-12"
    assert market.fx_session_date(et(2026, 10, 12, 17, 1)).isoformat() == "2026-10-13"
    # Friday 17:01 rolls over the whole weekend to Monday.
    assert market.fx_session_date(et(2026, 10, 9, 16, 59)).isoformat() == "2026-10-09"
    assert market.fx_session_date(et(2026, 10, 9, 17, 1)).isoformat() == "2026-10-12"
    # Weekend time belongs to Monday either side of the Sunday 17:00 open.
    assert market.fx_session_date(et(2026, 10, 10, 12, 0)).isoformat() == "2026-10-12"
    assert market.fx_session_date(et(2026, 10, 11, 16, 59)).isoformat() == "2026-10-12"
    assert market.fx_session_date(et(2026, 10, 11, 17, 1)).isoformat() == "2026-10-12"


# --- account creation -------------------------------------------------------------

def test_fx_account_defaults(conn):
    a20 = acct(conn, fx_account(conn, leverage=20, slip_zero=False))
    assert (a20["account_type"], a20["market"]) == ("margin", "fx")
    assert (a20["initial_margin_bps"], a20["maint_long_bps"], a20["maint_short_bps"]) == (500, 250, 250)
    assert (a20["margin_rate_bps"], a20["borrow_rate_bps"], a20["slippage_bps"], a20["commission_cents"]) == (0, 0, 1, 0)

    a10 = acct(conn, fx_account(conn, leverage=10, slip_zero=False))
    assert (a10["initial_margin_bps"], a10["maint_long_bps"], a10["maint_short_bps"]) == (1000, 500, 500)


def test_fx_leverage_only_10_or_20(conn):
    for lev in (10, 20):
        trading.create_account(conn, "live", "fx", 100_000_00, NOW, leverage=lev, market="fx")
    for lev in (5, 30):
        with pytest.raises(trading.TradeError, match="10x 或 20x"):
            trading.create_account(conn, "live", "fx", 100_000_00, NOW, leverage=lev, market="fx")


# --- order validation -------------------------------------------------------------

def test_fx_rejects_non_lot_qty(conn):
    aid = fx_account(conn)
    with pytest.raises(trading.TradeError, match="1000"):
        place(conn, aid, "EURUSD=X", "buy", 1500, 1.12)


def test_fx_lot_qty_fills(conn):
    aid = fx_account(conn)
    oid = place(conn, aid, "EURUSD=X", "buy", 1000, 1.12)
    assert status(conn, oid) == "filled"


def test_market_mismatch_rejected(conn):
    fx, us = fx_account(conn), us_account(conn)
    with pytest.raises(trading.TradeError, match="不能交易"):
        place(conn, fx, "AAPL", "buy", 10, 100)
    with pytest.raises(trading.TradeError, match="不能交易"):
        place(conn, us, "EURUSD=X", "buy", 1000, 1.12)


def test_fx_only_four_pairs(conn):
    aid = fx_account(conn)
    with pytest.raises(trading.TradeError, match="四个货币对"):
        place(conn, aid, "USDJPY=X", "buy", 1000, 150)  # =X but not in the whitelist
    for sym in ("EURUSD=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X"):
        assert status(conn, place(conn, aid, sym, "buy", 1000, 1.0)) == "filled"


# --- margin / financing -----------------------------------------------------------

def test_fx_liquidation_at_20x(conn):
    aid = fx_account(conn, leverage=20)
    # 20x: 5% initial, 2.5% maintenance. $100k equity -> $2M notional at 1.0.
    oid = place(conn, aid, "EURUSD=X", "buy", 2_000_000, 1.0)
    assert status(conn, oid) == "filled"
    # -2.5% -> equity $50k >= maint $48.75k: no call.
    assert trading.enforce_maintenance(conn, aid, {"EURUSD=X": q("EURUSD=X", 0.975)}, NOW) is None
    # -3% -> equity $40k < maint: liquidate.
    res = trading.enforce_maintenance(conn, aid, {"EURUSD=X": q("EURUSD=X", 0.97)}, NOW)
    assert res["closed"] == ["EURUSD=X"] and not res["bust"]
    assert res["equity"] == pytest.approx(40_000)
    assert trading.held_symbols(conn, aid) == []


def test_fx_no_financing(conn):
    aid = fx_account(conn, leverage=20)
    place(conn, aid, "EURUSD=X", "buy", 100_000, 1.0)
    cash_before = acct(conn, aid)["cash_cents"]
    marks = {"EURUSD=X": q("EURUSD=X", 1.0)}
    assert trading.accrue_financing(conn, aid, "2026-10-11", marks, NOW) == 0
    assert acct(conn, aid)["cash_cents"] == cash_before


# --- migration --------------------------------------------------------------------

V1_ACCOUNTS = """
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY, mode TEXT NOT NULL, account_type TEXT NOT NULL DEFAULT 'cash',
    status TEXT NOT NULL DEFAULT 'active', name TEXT NOT NULL, initial_cents INTEGER NOT NULL,
    cash_cents INTEGER NOT NULL, reserved_cents INTEGER NOT NULL DEFAULT 0,
    slippage_bps INTEGER NOT NULL DEFAULT 5, commission_cents INTEGER NOT NULL DEFAULT 0,
    initial_margin_bps INTEGER NOT NULL DEFAULT 10000, maint_long_bps INTEGER NOT NULL DEFAULT 0,
    maint_short_bps INTEGER NOT NULL DEFAULT 0, margin_rate_bps INTEGER NOT NULL DEFAULT 800,
    borrow_rate_bps INTEGER NOT NULL DEFAULT 300, accrued_through TEXT, start_date TEXT, sim_date TEXT,
    sim_phase TEXT, created_at TEXT NOT NULL);
INSERT INTO accounts (id, mode, name, initial_cents, cash_cents, created_at)
VALUES (1, 'live', '实时账户', 10000000, 9000000, 'x');
"""


def test_migrates_v1_to_v2_market_us(tmp_path):
    path = tmp_path / "v1.db"
    old = sqlite3.connect(path)
    old.executescript(V1_ACCOUNTS)
    old.execute("PRAGMA user_version = 1")
    old.close()
    c = db.connect(path)
    db.init(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert trading.get_account(c, 1)["market"] == "us"
    c.close()


# --- fx bar sanitizing ------------------------------------------------------------

def test_sanitize_fx_open_gap_and_extremes():
    rows = [
        ("EURUSD=X", "2020-01-02", 10000, 10100, 9900, 10050, 0),
        ("EURUSD=X", "2020-01-03", 20000, 30000, 5000, 10000, 0),  # 100% gap + absurd range
    ]
    out = replay._sanitize_fx(rows)
    assert out[0] == rows[0]
    # Open gap >5% is replaced with the previous close; high/low are clamped to 5% around the body.
    assert out[1] == ("EURUSD=X", "2020-01-03", 10050, 10552, 9500, 10000, 0)


# --- fx replay --------------------------------------------------------------------

FX_BARS = [("2020-01-17", 11000, 11100, 10900, 11050),
           ("2020-01-20", 11050, 11200, 11000, 11150),   # MLK Day: US closed, FX trades
           ("2020-01-21", 11150, 11200, 11000, 11100)]


class NoNetwork:
    def daily_bars(self, symbol, period):
        raise AssertionError("tests must not hit the network")


@pytest.fixture
def fx_replay_conn(conn):
    conn.executemany("INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, 1000)",
                     [("EURUSD=X", d, o, h, l, c) for d, o, h, l, c in FX_BARS])
    conn.execute("INSERT INTO bar_meta VALUES (?, ?, ?, ?)",
                 ("EURUSD=X", FX_BARS[0][0], FX_BARS[-1][0], iso(datetime.now(timezone.utc))))
    return conn


def test_fx_replay_advances_through_us_holiday(fx_replay_conn):
    aid = replay.new_game(fx_replay_conn, NoNetwork(), 100_000_00, "2020-01-17", market="fx", leverage=10)
    fx_replay_conn.execute("UPDATE accounts SET slippage_bps = 0 WHERE id = ?", (aid,))
    a = acct(fx_replay_conn, aid)
    assert (a["market"], a["sim_date"]) == ("fx", "2020-01-17")
    ev = replay.advance(fx_replay_conn, aid, 3)  # 01-17 close -> 01-20 open -> 01-20 close
    assert (ev["sim_date"], ev["sim_phase"]) == ("2020-01-20", "close")


def test_fx_replay_benchmark_usd_cash(fx_replay_conn):
    aid = replay.new_game(fx_replay_conn, NoNetwork(), 100_000_00, "2020-01-17", market="fx", leverage=10)
    out = api.get_portfolio(aid, conn=fx_replay_conn)
    assert out["benchmark_name"] == "USD 现金"
    assert out["benchmark_return"] == 0.0


def test_fx_new_game_not_before_2010(fx_replay_conn):
    fx_replay_conn.execute("INSERT INTO daily_bars VALUES ('EURUSD=X', '2009-12-31', 10000, 10100, 9900, 10050, 1000)")
    aid = replay.new_game(fx_replay_conn, NoNetwork(), 100_000_00, "2009-12-31", market="fx", leverage=10)
    start = acct(fx_replay_conn, aid)["start_date"]
    assert start >= "2010-01-04" and start != "2009-12-31"
