"""Order placement, matching and bookkeeping.

Pure database logic: callers fetch quotes (network) beforehand and pass them in,
so no transaction ever waits on yfinance. Live and replay accounts share this engine;
the only difference is where quotes, trade dates and `now` come from.
"""
import sqlite3
from datetime import datetime

from . import db
from .models import Quote, buy_cents, iso, sell_cents, slip_down, slip_up


class TradeError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def create_account(conn, mode, name, initial_cents, now: datetime, **replay) -> int:
    with db.tx(conn):
        cur = conn.execute(
            "INSERT INTO accounts (mode, name, initial_cents, cash_cents, start_date, sim_date, sim_phase, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mode, name, initial_cents, initial_cents, replay.get("start_date"),
             replay.get("sim_date"), replay.get("sim_phase"), iso(now)))
        account_id = cur.lastrowid
        _ledger(conn, account_id, initial_cents, "deposit", None, now)
        return account_id


def get_account(conn, account_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise TradeError("账户不存在", 404)
    return row


def reset_account(conn, account_id: int, now: datetime) -> None:
    with db.tx(conn):
        acct = get_account(conn, account_id)
        for table in ("cash_ledger", "fills", "orders", "positions"):
            conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
        conn.execute("UPDATE accounts SET cash_cents = initial_cents, reserved_cents = 0 WHERE id = ?", (account_id,))
        _ledger(conn, account_id, acct["initial_cents"], "deposit", None, now)


def place_order(conn, account_id: int, symbol: str, side: str, otype: str, qty: int,
                limit_price4: int | None, quote: Quote | None, trade_date: str, now: datetime) -> int:
    """Validate and record an order; market orders fill immediately or are rejected.

    Returns the order id. Business rejections (closed market, insufficient funds) are
    recorded as `rejected` orders so they show up in history; malformed input raises.
    """
    if side not in ("buy", "sell"):
        raise TradeError("side 必须是 buy 或 sell")
    if otype not in ("market", "limit"):
        raise TradeError("type 必须是 market 或 limit")
    if not isinstance(qty, int) or qty <= 0:
        raise TradeError("数量必须是正整数")
    if otype == "limit" and (limit_price4 is None or limit_price4 <= 0):
        raise TradeError("限价单需要正的限价")
    if otype == "market":
        limit_price4 = None

    with db.tx(conn):
        acct = get_account(conn, account_id)
        if acct["status"] != "active":
            raise TradeError("该存档已结束，不能下单")

        def insert(status, reason=None, reserved=0):
            cur = conn.execute(
                "INSERT INTO orders (account_id, symbol, side, type, qty, limit_price4, status, reason,"
                " reserved_cents, trade_date, placed_at, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, symbol, side, otype, qty, limit_price4, status, reason, reserved, trade_date,
                 iso(now), iso(now) if status != "open" else None))
            return cur.lastrowid

        tradable = quote is not None and quote.tradable
        if otype == "market" and not tradable:
            return insert("rejected", quote.reason if quote else "暂无该股票行情")

        bps, fee = acct["slippage_bps"], acct["commission_cents"]
        available_cash = acct["cash_cents"] - acct["reserved_cents"]
        pos = _position(conn, account_id, symbol)
        available_qty = pos["qty"] - pos["reserved_qty"] if pos else 0
        if side == "sell" and qty > available_qty:
            return insert("rejected", f"可用持仓不足（可卖 {available_qty} 股）")

        if otype == "market":
            price4 = slip_up(quote.price4, bps) if side == "buy" else slip_down(quote.price4, bps)
            if side == "buy" and buy_cents(qty, price4) + fee > available_cash:
                return insert("rejected", "可用资金不足")
            order_id = insert("open")
            _fill(conn, order_id, price4, quote.as_of, now)
            return order_id

        reserved = 0
        if side == "buy":
            reserved = buy_cents(qty, limit_price4) + fee
            if reserved > available_cash:
                return insert("rejected", "可用资金不足")
            conn.execute("UPDATE accounts SET reserved_cents = reserved_cents + ? WHERE id = ?", (reserved, account_id))
        else:
            conn.execute("UPDATE positions SET reserved_qty = reserved_qty + ? WHERE account_id = ? AND symbol = ?",
                         (qty, account_id, symbol))
        order_id = insert("open", reserved=reserved)
        if tradable:
            _try_match(conn, order_id, quote, now)
        return order_id


def cancel_order(conn, account_id: int, order_id: int, now: datetime) -> None:
    with db.tx(conn):
        order = conn.execute("SELECT * FROM orders WHERE id = ? AND account_id = ?", (order_id, account_id)).fetchone()
        if order is None:
            raise TradeError("订单不存在", 404)
        if order["status"] != "open":
            raise TradeError("订单已不是待成交状态")
        _close_open(conn, order, "cancelled", None, now)


def expire_orders(conn, account_id: int, before_date: str, now: datetime) -> int:
    """Expire DAY orders whose trade_date is earlier than `before_date` (YYYY-MM-DD)."""
    with db.tx(conn):
        rows = conn.execute("SELECT * FROM orders WHERE account_id = ? AND status = 'open' AND trade_date < ?",
                            (account_id, before_date)).fetchall()
        for order in rows:
            _close_open(conn, order, "expired", "当日有效订单已过期", now)
        return len(rows)


def match_open_orders(conn, account_id: int, quotes: dict[str, Quote], now: datetime) -> int:
    """Check open limit orders against point-in-time quotes."""
    filled = 0
    for order in open_orders(conn, account_id):
        q = quotes.get(order["symbol"])
        if q is None or not q.tradable:
            continue
        with db.tx(conn):
            filled += _try_match(conn, order["id"], q, now)
    return filled


def match_intraday(conn, account_id: int, bars: dict[str, sqlite3.Row], as_of: datetime, now: datetime) -> int:
    """Replay: fill limit orders whose price was touched within a daily bar, at the limit price."""
    filled = 0
    for order in open_orders(conn, account_id):
        bar = bars.get(order["symbol"])
        if bar is None:
            continue
        limit = order["limit_price4"]
        if (order["side"] == "buy" and bar["low4"] <= limit) or (order["side"] == "sell" and bar["high4"] >= limit):
            with db.tx(conn):
                if conn.execute("SELECT status FROM orders WHERE id = ?", (order["id"],)).fetchone()["status"] == "open":
                    _fill(conn, order["id"], limit, as_of, now)
                    filled += 1
    return filled


def open_orders(conn, account_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM orders WHERE account_id = ? AND status = 'open' ORDER BY id",
                        (account_id,)).fetchall()


def held_symbols(conn, account_id: int) -> list[str]:
    return [r["symbol"] for r in conn.execute(
        "SELECT symbol FROM positions WHERE account_id = ? AND qty > 0 ORDER BY symbol", (account_id,))]


def portfolio(conn, account_id: int, marks: dict[str, Quote | None]) -> dict:
    acct = get_account(conn, account_id)
    positions, market_value, realized = [], 0, 0
    for p in conn.execute("SELECT * FROM positions WHERE account_id = ? ORDER BY symbol", (account_id,)):
        realized += p["realized_cents"]
        if p["qty"] == 0:
            continue
        mark = marks.get(p["symbol"])
        value = sell_cents(p["qty"], mark.price4) if mark else None
        if value is not None:
            market_value += value
        positions.append({
            "symbol": p["symbol"],
            "qty": p["qty"],
            "reserved_qty": p["reserved_qty"],
            "avg_cost": p["cost_cents"] / p["qty"] / 100,
            "cost": p["cost_cents"] / 100,
            "price": mark.price4 / 10000 if mark else None,
            "price_as_of": iso(mark.as_of) if mark else None,
            "market_value": value / 100 if value is not None else None,
            "unrealized": (value - p["cost_cents"]) / 100 if value is not None else None,
            "realized": p["realized_cents"] / 100,
        })
    missing = [p["symbol"] for p in positions if p["price"] is None]
    equity = acct["cash_cents"] + market_value
    return {
        "account_id": account_id,
        "cash": acct["cash_cents"] / 100,
        "reserved": acct["reserved_cents"] / 100,
        "available_cash": (acct["cash_cents"] - acct["reserved_cents"]) / 100,
        "market_value": market_value / 100,
        "equity": equity / 100,
        "initial": acct["initial_cents"] / 100,
        "total_return": (equity - acct["initial_cents"]) / acct["initial_cents"],
        "realized": realized / 100,
        "positions": positions,
        "unpriced_symbols": missing,
    }


# --- internals -----------------------------------------------------------------

def _position(conn, account_id, symbol):
    return conn.execute("SELECT * FROM positions WHERE account_id = ? AND symbol = ?", (account_id, symbol)).fetchone()


def _try_match(conn, order_id: int, quote: Quote, now: datetime) -> bool:
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order["status"] != "open" or order["type"] != "limit":
        return False
    bps = get_account(conn, order["account_id"])["slippage_bps"]
    limit, p = order["limit_price4"], quote.price4
    if order["side"] == "buy" and p <= limit:
        _fill(conn, order_id, min(limit, slip_up(p, bps)), quote.as_of, now)
        return True
    if order["side"] == "sell" and p >= limit:
        _fill(conn, order_id, max(limit, slip_down(p, bps)), quote.as_of, now)
        return True
    return False


def _fill(conn, order_id: int, price4: int, quote_as_of: datetime, now: datetime) -> None:
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    account_id, symbol, qty = order["account_id"], order["symbol"], order["qty"]
    fee = get_account(conn, account_id)["commission_cents"]

    if order["side"] == "buy":
        amount = buy_cents(qty, price4)
        cash_delta = -(amount + fee)
        conn.execute("UPDATE accounts SET cash_cents = cash_cents + ?, reserved_cents = reserved_cents - ? WHERE id = ?",
                     (cash_delta, order["reserved_cents"], account_id))
        conn.execute(
            "INSERT INTO positions (account_id, symbol, qty, cost_cents) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (account_id, symbol) DO UPDATE SET qty = qty + excluded.qty,"
            " cost_cents = cost_cents + excluded.cost_cents",
            (account_id, symbol, qty, amount + fee))
    else:
        pos = _position(conn, account_id, symbol)
        amount = sell_cents(qty, price4)
        cash_delta = amount - fee
        if qty == pos["qty"]:
            cost_part = pos["cost_cents"]
        else:
            cost_part = (pos["cost_cents"] * qty * 2 + pos["qty"]) // (2 * pos["qty"])
        released = qty if order["type"] == "limit" else 0
        conn.execute(
            "UPDATE positions SET qty = qty - ?, reserved_qty = reserved_qty - ?, cost_cents = cost_cents - ?,"
            " realized_cents = realized_cents + ? WHERE account_id = ? AND symbol = ?",
            (qty, released, cost_part, cash_delta - cost_part, account_id, symbol))
        conn.execute("UPDATE accounts SET cash_cents = cash_cents + ? WHERE id = ?", (cash_delta, account_id))

    fill_id = conn.execute(
        "INSERT INTO fills (order_id, account_id, symbol, side, qty, price4, amount_cents, fees_cents,"
        " quote_as_of, executed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, account_id, symbol, order["side"], qty, price4, amount, fee, iso(quote_as_of), iso(now))).lastrowid
    _ledger(conn, account_id, cash_delta, order["side"], fill_id, now)
    conn.execute("UPDATE orders SET status = 'filled', fill_price4 = ?, closed_at = ? WHERE id = ?",
                 (price4, iso(now), order_id))


def _close_open(conn, order, status: str, reason: str | None, now: datetime) -> None:
    if order["side"] == "buy":
        conn.execute("UPDATE accounts SET reserved_cents = reserved_cents - ? WHERE id = ?",
                     (order["reserved_cents"], order["account_id"]))
    else:
        conn.execute("UPDATE positions SET reserved_qty = reserved_qty - ? WHERE account_id = ? AND symbol = ?",
                     (order["qty"], order["account_id"], order["symbol"]))
    conn.execute("UPDATE orders SET status = ?, reason = ?, closed_at = ? WHERE id = ?",
                 (status, reason, iso(now), order["id"]))


def _ledger(conn, account_id, amount, category, fill_id, now) -> None:
    balance = conn.execute("SELECT cash_cents FROM accounts WHERE id = ?", (account_id,)).fetchone()["cash_cents"]
    conn.execute("INSERT INTO cash_ledger (account_id, amount_cents, balance_cents, category, fill_id, created_at)"
                 " VALUES (?, ?, ?, ?, ?, ?)", (account_id, amount, balance, category, fill_id, iso(now)))
