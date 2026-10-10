from datetime import datetime, timezone

import pytest

from app import db, replay, trading
from app.models import iso

# (date, open, high, low, close)
SPY = [("2020-01-02", 100, 102, 99, 101), ("2020-01-03", 101, 103, 100, 102),
       ("2020-01-06", 102, 104, 101, 103), ("2020-01-07", 103, 105, 102, 104)]
XYZ = [("2020-01-02", 50, 52, 48, 51), ("2020-01-03", 51, 51, 45, 46),
       ("2020-01-06", 40, 42, 39, 41), ("2020-01-07", 41, 60, 41, 59)]


class NoNetwork:
    def daily_bars(self, symbol, period):
        raise AssertionError("tests must not hit the network")


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    for sym, rows in (("SPY", SPY), ("XYZ", XYZ)):
        c.executemany("INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, 1000)",
                      [(sym, d, o * 10000, h * 10000, l * 10000, cl * 10000) for d, o, h, l, cl in rows])
        c.execute("INSERT INTO bar_meta VALUES (?, ?, ?, ?)",
                  (sym, rows[0][0], rows[-1][0], iso(datetime.now(timezone.utc))))
    yield c
    c.close()


@pytest.fixture
def game(conn):
    aid = replay.new_game(conn, NoNetwork(), 10_000_00, "2020-01-02")
    conn.execute("UPDATE accounts SET slippage_bps = 0 WHERE id = ?", (aid,))
    return aid


def acct(conn, aid):
    return trading.get_account(conn, aid)


def place(conn, aid, side, otype, qty, limit=None):
    a = acct(conn, aid)
    return trading.place_order(conn, aid, "XYZ", side, otype, qty, limit * 10000 if limit else None,
                               replay.quote(conn, a, "XYZ"), replay.session_date(conn, a), replay.now_of(a))


def status(conn, oid):
    return conn.execute("SELECT status, fill_price4 FROM orders WHERE id = ?", (oid,)).fetchone()


def test_new_game_archives_previous(conn, game):
    g2 = replay.new_game(conn, NoNetwork(), 10_000_00, "2020-01-03")
    assert acct(conn, game)["status"] == "archived"
    a = acct(conn, g2)
    assert (a["sim_date"], a["sim_phase"], a["status"]) == ("2020-01-03", "open", "active")


def test_market_buy_at_open_then_close(conn, game):
    oid = place(conn, game, "buy", "market", 10)
    assert status(conn, oid)["fill_price4"] == 50_0000
    replay.advance(conn, game, 1)
    oid = place(conn, game, "sell", "market", 10)
    assert status(conn, oid)["fill_price4"] == 51_0000
    assert acct(conn, game)["cash_cents"] == 10_000_00 + 10_00


def test_no_future_data(conn, game):
    bars = replay.bars(conn, acct(conn, game), "XYZ", 100)
    assert len(bars) == 1 and bars[0]["close"] == bars[0]["open"] == 50
    replay.advance(conn, game, 1)
    bars = replay.bars(conn, acct(conn, game), "XYZ", 100)
    assert len(bars) == 1 and bars[0]["close"] == 51


def test_limit_buy_fills_intraday_at_limit(conn, game):
    replay.advance(conn, game, 2)  # 2020-01-03 open @51
    oid = place(conn, game, "buy", "limit", 10, limit=47)
    assert status(conn, oid)["status"] == "open"
    replay.advance(conn, game, 1)  # low 45 touches 47
    assert tuple(status(conn, oid)) == ("filled", 47_0000)


def test_limit_buy_gap_down_fills_at_open(conn, game):
    replay.advance(conn, game, 1)  # 2020-01-02 close; order belongs to next session
    oid = place(conn, game, "buy", "limit", 10, limit=44)
    assert status(conn, oid)["status"] == "open"
    replay.advance(conn, game, 1)  # 01-03 open 51: no fill
    assert status(conn, oid)["status"] == "open"
    replay.advance(conn, game, 1)  # 01-03 low 45: no fill, then expires
    assert status(conn, oid)["status"] == "expired"
    assert acct(conn, game)["reserved_cents"] == 0


def test_order_at_close_survives_to_next_open(conn, game):
    replay.advance(conn, game, 3)  # 2020-01-03 close
    oid = place(conn, game, "buy", "limit", 10, limit=42)
    replay.advance(conn, game, 1)  # 01-06 opens at 40 < 42 -> fills at open
    assert tuple(status(conn, oid)) == ("filled", 40_0000)


def test_limit_sell_intraday(conn, game):
    place(conn, game, "buy", "market", 10)
    replay.advance(conn, game, 6)  # 2020-01-07 open @41
    oid = place(conn, game, "sell", "limit", 10, limit=55)
    replay.advance(conn, game, 1)
    assert tuple(status(conn, oid)) == ("filled", 55_0000)


def test_game_finishes_at_end_of_data(conn, game):
    ev = replay.advance(conn, game, 100)
    assert ev["finished"] and ev["status"] == "finished"
    assert (ev["sim_date"], ev["sim_phase"]) == ("2020-01-07", "close")
    with pytest.raises(trading.TradeError):
        place(conn, game, "buy", "market", 1)


def test_mark_uses_last_close_when_no_bar(conn, game):
    conn.execute("DELETE FROM daily_bars WHERE symbol = 'XYZ' AND date = '2020-01-03'")
    replay.advance(conn, game, 2)
    a = acct(conn, game)
    assert replay.mark(conn, a, "XYZ").price4 == 51_0000
    q = replay.quote(conn, a, "XYZ")
    assert q is not None and not q.tradable


def test_week_step(conn, game):
    ev = replay.advance(conn, game, replay.STEPS["day"])
    assert (ev["sim_date"], ev["sim_phase"]) == ("2020-01-03", "open")
