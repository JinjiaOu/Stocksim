"""Historical replay ("offline mode"): trade through past daily bars, one half-session at a time.

Each replay account has a simulated position in time: (sim_date, phase), phase being the
open or the close of that trading day. Quotes are that bar's open or close; nothing after
the current point is ever exposed. Prices are split/dividend-adjusted, so corporate actions
need no separate handling. The trading calendar is SPY's daily bar dates.
"""
import random
from datetime import date, datetime, timedelta, timezone

from . import db
from .market import CLOSE_T, ET, OPEN_T, MarketDataProvider
from .models import Quote, iso, to_price4, utcnow
from .trading import (TradeError, accrue_financing, create_account, enforce_maintenance, expire_orders,
                      get_account, held_symbols, match_intraday, match_open_orders, open_orders)

CALENDAR = "SPY"
EARLIEST_START = "2000-01-03"
MIN_RUNWAY_DAYS = 252  # random starts leave at least a year of history to play
STEPS = {"half": 1, "day": 2, "week": 10, "month": 42}


def ensure_daily(conn, provider: MarketDataProvider, symbol: str, need_through: str | None = None) -> bool:
    """Make sure full daily history for `symbol` is cached. Returns False if no data exists.

    Cached history is kept as-is during a game (re-downloading would re-adjust past prices);
    it is only refreshed when a game needs dates beyond the cached range.
    """
    meta = conn.execute("SELECT * FROM bar_meta WHERE symbol = ?", (symbol,)).fetchone()
    if meta and (need_through is None or meta["last_date"] >= need_through):
        return True
    if meta and datetime.fromisoformat(meta["fetched_at"]) > utcnow() - timedelta(hours=12):
        return True
    df = provider.daily_bars(symbol, "max")
    if df is None:
        return meta is not None
    rows = [(symbol, ts.date().isoformat(), to_price4(r.Open), to_price4(r.High), to_price4(r.Low),
             to_price4(r.Close), int(r.Volume)) for ts, r in zip(df.index, df.itertuples())]
    with db.tx(conn):
        conn.execute("DELETE FROM daily_bars WHERE symbol = ?", (symbol,))
        conn.executemany("INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        conn.execute("INSERT OR REPLACE INTO bar_meta VALUES (?, ?, ?, ?)",
                     (symbol, rows[0][1], rows[-1][1], iso(utcnow())))
    return True


def new_game(conn, provider: MarketDataProvider, initial_cents: int, start_date: str | None = None,
             rng: random.Random | None = None, account_type: str = "cash", leverage: float = 1.0,
             market: str = "us") -> int:
    if not ensure_daily(conn, provider, CALENDAR):
        raise TradeError("无法获取交易日历（SPY 历史数据）", 503)
    dates = [r["date"] for r in conn.execute("SELECT date FROM daily_bars WHERE symbol = ? ORDER BY date",
                                             (CALENDAR,))]
    if start_date:
        candidates = [d for d in dates[:-1] if d >= start_date]
        if not candidates:
            raise TradeError("开始日期太晚，没有可回放的历史数据")
        start = candidates[0]
    else:
        pool = [d for d in dates[:-MIN_RUNWAY_DAYS] if d >= EARLIEST_START]
        start = (rng or random).choice(pool)
    with db.tx(conn):
        conn.execute("UPDATE accounts SET status = 'archived' WHERE mode = 'replay' AND status != 'archived'")
        return create_account(conn, "replay", f"回放 {start}", initial_cents, utcnow(), account_type=account_type,
                              leverage=leverage, start_date=start, sim_date=start, sim_phase="open",
                              accrued_through=start, market=market)


def sim_time(d: str, phase: str) -> datetime:
    t = OPEN_T if phase == "open" else CLOSE_T
    return datetime.combine(date.fromisoformat(d), t, ET).astimezone(timezone.utc)


def now_of(acct) -> datetime:
    return sim_time(acct["sim_date"], acct["sim_phase"])


def next_trading_date(conn, d: str) -> str | None:
    row = conn.execute("SELECT MIN(date) AS d FROM daily_bars WHERE symbol = ? AND date > ?", (CALENDAR, d)).fetchone()
    return row["d"]


def session_date(conn, acct) -> str:
    """Orders placed at the open belong to today's session; at the close, to the next one."""
    if acct["sim_phase"] == "open":
        return acct["sim_date"]
    return next_trading_date(conn, acct["sim_date"]) or acct["sim_date"]


def _bar(conn, symbol: str, d: str):
    return conn.execute("SELECT * FROM daily_bars WHERE symbol = ? AND date = ?", (symbol, d)).fetchone()


def quote(conn, acct, symbol: str) -> Quote | None:
    """Tradable quote at the current simulated moment, or an untradable one explaining why."""
    bar = _bar(conn, symbol, acct["sim_date"])
    when = now_of(acct)
    if bar is None:
        last = mark(conn, acct, symbol)
        if last is None:
            return None
        return Quote(symbol, last.price4, last.as_of, "replay", tradable=False, reason="该股票当日无行情（未上市或停牌）")
    if acct["status"] != "active":
        return Quote(symbol, bar["close4"], when, "replay", tradable=False, reason="回放已结束")
    price = bar["open4"] if acct["sim_phase"] == "open" else bar["close4"]
    return Quote(symbol, price, when, "replay")


def mark(conn, acct, symbol: str) -> Quote | None:
    """Latest known price at the simulated moment (for valuation), tradable or not."""
    bar = _bar(conn, symbol, acct["sim_date"])
    if bar is not None:
        price = bar["open4"] if acct["sim_phase"] == "open" else bar["close4"]
        return Quote(symbol, price, now_of(acct), "replay", tradable=False)
    row = conn.execute("SELECT date, close4 FROM daily_bars WHERE symbol = ? AND date < ? ORDER BY date DESC LIMIT 1",
                       (symbol, acct["sim_date"])).fetchone()
    if row is None:
        return None
    return Quote(symbol, row["close4"], sim_time(row["date"], "close"), "replay", tradable=False)


def bars(conn, acct, symbol: str, limit: int) -> list[dict]:
    """Daily bars up to the simulated moment; at the open, today's bar shows only the open price."""
    rows = conn.execute("SELECT * FROM daily_bars WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT ?",
                        (symbol, acct["sim_date"], limit)).fetchall()[::-1]
    out = []
    for r in rows:
        b = {"time": r["date"], "open": r["open4"] / 1e4, "high": r["high4"] / 1e4, "low": r["low4"] / 1e4,
             "close": r["close4"] / 1e4, "volume": r["volume"]}
        if r["date"] == acct["sim_date"] and acct["sim_phase"] == "open":
            b.update(high=b["open"], low=b["open"], close=b["open"], volume=0)
        out.append(b)
    return out


def _check_margin(conn, account_id: int, events: dict) -> None:
    acct = get_account(conn, account_id)
    quotes = {s: q for s in held_symbols(conn, account_id) if (q := quote(conn, acct, s))}
    result = enforce_maintenance(conn, account_id, quotes, now_of(acct))
    if result:
        events["liquidated"] += len(result["closed"])
        events["bust"] = result["bust"]
        events.setdefault("margin_calls", []).append({"date": acct["sim_date"], **result})


def advance(conn, account_id: int, half_steps: int) -> dict:
    """Move the simulated clock forward, matching and expiring orders along the way.

    Margin accounts are checked against maintenance at every open and close snapshot
    (intraday extremes are not checked), and financing accrues on each overnight step.
    """
    events = {"filled": 0, "expired": 0, "liquidated": 0, "financing": 0.0, "bust": False, "finished": False}
    for _ in range(half_steps):
        acct = get_account(conn, account_id)
        if acct["mode"] != "replay":
            raise TradeError("只有回放存档可以推进时间")
        if acct["status"] != "active":
            events["finished"] = True
            break
        d = acct["sim_date"]
        if acct["sim_phase"] == "open":
            # Open -> close: anything touched during the day fills at its limit, then DAY orders lapse.
            close = sim_time(d, "close")
            day_bars = {o["symbol"]: b for o in open_orders(conn, account_id) if (b := _bar(conn, o["symbol"], d))}
            events["filled"] += match_intraday(conn, account_id, day_bars, close, close)
            events["expired"] += expire_orders(conn, account_id, (date.fromisoformat(d) + timedelta(days=1)).isoformat(),
                                               close)
            with db.tx(conn):
                conn.execute("UPDATE accounts SET sim_phase = 'close' WHERE id = ?", (account_id,))
            _check_margin(conn, account_id, events)
        else:
            nxt = next_trading_date(conn, d)
            if nxt is None:
                with db.tx(conn):
                    conn.execute("UPDATE accounts SET status = 'finished' WHERE id = ?", (account_id,))
                events["finished"] = True
                break
            marks = {s: mark(conn, acct, s) for s in held_symbols(conn, account_id)}
            events["financing"] += accrue_financing(conn, account_id, nxt, marks, sim_time(nxt, "open")) / 100
            with db.tx(conn):
                conn.execute("UPDATE accounts SET sim_date = ?, sim_phase = 'open' WHERE id = ?", (nxt, account_id))
            acct = get_account(conn, account_id)
            quotes = {o["symbol"]: q for o in open_orders(conn, account_id) if (q := quote(conn, acct, o["symbol"]))}
            events["filled"] += match_open_orders(conn, account_id, quotes, now_of(acct))
            _check_margin(conn, account_id, events)
        if events["bust"]:
            events["finished"] = True
            break
    acct = get_account(conn, account_id)
    events.update(sim_date=acct["sim_date"], sim_phase=acct["sim_phase"], status=acct["status"])
    return events
