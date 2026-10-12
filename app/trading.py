"""Order placement, matching and bookkeeping.

Pure database logic: callers fetch quotes (network) beforehand and pass them in,
so no transaction ever waits on yfinance. Live and replay accounts share this engine;
the only difference is where quotes, trade dates and `now` come from.

Two account types:
- cash:   long only, every purchase fully paid; limit orders reserve cash / shares.
- margin: long and short. Positions are signed (qty < 0 = short) and cash may go negative
          (a margin loan). Opening exposure needs `initial_margin_bps` of its value in excess
          equity; if equity falls below the maintenance requirement, open orders are cancelled
          and positions are force-closed worst-first.

Equity is always cash + Σ qty × price; a short's negative value is its buy-back liability.
"""
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from . import db
from .models import Quote, buy_cents, iso, sell_cents, slip_down, slip_up

DAY_COUNT = 360  # money-market convention for interest


class TradeError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def value_cents(qty: int, price4: int) -> int:
    """Signed market value, rounded against the player (longs down, short liabilities up)."""
    return sell_cents(qty, price4) if qty >= 0 else -buy_cents(-qty, price4)


def _ceil_bps(amount: int, bps: int) -> int:
    return -(-amount * bps // 10000)


def margin_params(leverage: float) -> dict:
    """Initial margin 1/leverage; maintenance half of that for longs, 60% of it for shorts (2x → Reg T 50/25/30)."""
    initial = round(10000 / leverage)
    return {"initial_margin_bps": initial, "maint_long_bps": initial // 2, "maint_short_bps": initial * 6 // 10}


# Forex accounts: always margin, symmetric maintenance, ~1 pip spread, no financing (overnight swap is out of scope).
FX_LEVERAGES = (10, 20)
FX_DEFAULTS = {"slippage_bps": 1, "margin_rate_bps": 0, "borrow_rate_bps": 0}
FX_LOT = 1000  # orders are whole micro lots
# v1: USD-quoted majors only, so price × units is already in dollars and 4 decimals is one pip.
FX_PAIRS = ("EURUSD=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X")


def symbol_market(symbol: str) -> str:
    return "fx" if symbol.endswith("=X") else "us"


def create_account(conn, mode, name, initial_cents, now: datetime, account_type="cash", leverage=1.0,
                   start_date=None, sim_date=None, sim_phase=None, accrued_through=None, market="us") -> int:
    if market == "fx":
        if leverage not in FX_LEVERAGES:
            raise TradeError("外汇账户杠杆只能是 10x 或 20x")
        account_type = "margin"
        params = margin_params(leverage)
        params["maint_short_bps"] = params["maint_long_bps"]
        extra = FX_DEFAULTS
    else:
        params = margin_params(leverage) if account_type == "margin" else margin_params(1) | {
            "maint_long_bps": 0, "maint_short_bps": 0}
        extra = {}
    cols = ("mode", "account_type", "market", "name", "initial_cents", "cash_cents", "initial_margin_bps",
            "maint_long_bps", "maint_short_bps", "accrued_through", "start_date", "sim_date", "sim_phase",
            "created_at", *extra)
    values = (mode, account_type, market, name, initial_cents, initial_cents, params["initial_margin_bps"],
              params["maint_long_bps"], params["maint_short_bps"], accrued_through, start_date, sim_date,
              sim_phase, iso(now), *extra.values())
    with db.tx(conn):
        cur = conn.execute(f"INSERT INTO accounts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           values)
        account_id = cur.lastrowid
        _ledger(conn, account_id, initial_cents, "deposit", None, now)
        return account_id


def get_account(conn, account_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise TradeError("账户不存在", 404)
    return row


def reset_account(conn, account_id: int, now: datetime, accrued_through: str | None = None) -> None:
    with db.tx(conn):
        acct = get_account(conn, account_id)
        for table in ("cash_ledger", "fills", "orders", "positions"):
            conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
        conn.execute("UPDATE accounts SET cash_cents = initial_cents, reserved_cents = 0, status = 'active',"
                     " accrued_through = ? WHERE id = ?", (accrued_through, account_id))
        _ledger(conn, account_id, acct["initial_cents"], "deposit", None, now)


# --- margin math ----------------------------------------------------------------

@dataclass
class MarginState:
    equity: int
    long_value: int
    short_value: int          # positive number: buy-back cost of all shorts
    initial_req: int
    maint_req: int
    reserved: int
    missing: list[str] = field(default_factory=list)

    @property
    def excess(self) -> int:
        """Equity available to back new exposure."""
        return self.equity - self.initial_req - self.reserved


def margin_state(conn, acct, marks: dict[str, Quote | None] | None) -> MarginState:
    marks = marks or {}
    long_v = short_v = 0
    maint = 0
    missing = []
    for p in _positions(conn, acct["id"]):
        m = marks.get(p["symbol"])
        if m is None:
            missing.append(p["symbol"])
            continue
        v = value_cents(p["qty"], m.price4)
        if v >= 0:
            long_v += v
            maint += _ceil_bps(v, acct["maint_long_bps"])
        else:
            short_v -= v
            maint += _ceil_bps(-v, acct["maint_short_bps"])
    return MarginState(
        equity=acct["cash_cents"] + long_v - short_v, long_value=long_v, short_value=short_v,
        initial_req=_ceil_bps(long_v + short_v, acct["initial_margin_bps"]), maint_req=maint,
        reserved=acct["reserved_cents"], missing=missing)


def _is_reducing(pos_qty: int, new_qty: int) -> bool:
    return pos_qty != 0 and abs(new_qty) < abs(pos_qty) and (new_qty == 0 or (new_qty > 0) == (pos_qty > 0))


# --- orders ---------------------------------------------------------------------

def place_order(conn, account_id: int, symbol: str, side: str, otype: str, qty: int,
                limit_price4: int | None, quote: Quote | None, trade_date: str, now: datetime,
                marks: dict[str, Quote | None] | None = None) -> int:
    """Validate and record an order; market orders fill immediately or are rejected.

    `marks` (latest prices of every held symbol) is required for margin accounts to size margin.
    Returns the order id. Business rejections (closed market, insufficient funds/margin) are
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
        if symbol_market(symbol) != acct["market"]:
            raise TradeError("该账户不能交易此品种")
        if acct["market"] == "fx":
            if symbol not in FX_PAIRS:
                raise TradeError("目前只支持 EURUSD、GBPUSD、AUDUSD、NZDUSD 四个货币对")
            if qty % FX_LOT:
                raise TradeError("外汇按 1000 单位（微型手）的整数倍下单")

        def insert(status, reason=None, reserved=0):
            cur = conn.execute(
                "INSERT INTO orders (account_id, symbol, side, type, qty, limit_price4, status, reason,"
                " reserved_cents, trade_date, placed_at, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, symbol, side, otype, qty, limit_price4, status, reason, reserved, trade_date,
                 iso(now), iso(now) if status != "open" else None))
            return cur.lastrowid

        tradable = quote is not None and quote.tradable
        if otype == "market" and not tradable:
            return insert("rejected", quote.reason if quote else "暂无该品种行情")

        bps, fee = acct["slippage_bps"], acct["commission_cents"]
        price4 = None
        if otype == "market":
            price4 = slip_up(quote.price4, bps) if side == "buy" else slip_down(quote.price4, bps)
        ref4 = price4 or limit_price4
        pos = _position(conn, account_id, symbol)
        pos_qty = pos["qty"] if pos else 0
        reserved = 0

        if acct["account_type"] == "cash":
            available_qty = pos_qty - pos["reserved_qty"] if pos else 0
            if side == "sell" and qty > available_qty:
                return insert("rejected", f"可用持仓不足（现金账户不能卖空，可卖 {max(available_qty, 0)} 股）")
            if side == "buy":
                cost = buy_cents(qty, ref4) + fee
                if cost > acct["cash_cents"] - acct["reserved_cents"]:
                    return insert("rejected", "可用资金不足")
                if otype == "limit":
                    reserved = cost
            elif otype == "limit":
                conn.execute("UPDATE positions SET reserved_qty = reserved_qty + ? WHERE account_id = ? AND symbol = ?",
                             (qty, account_id, symbol))
        else:
            new_qty = pos_qty + (qty if side == "buy" else -qty)
            if not _is_reducing(pos_qty, new_qty):
                st = margin_state(conn, acct, marks)
                if st.missing:
                    return insert("rejected", f"无法给持仓估值（{', '.join(st.missing)}），暂不能开新仓")
                cur_mark = marks[symbol].price4 if pos_qty else ref4
                need = max(0, _ceil_bps(abs(value_cents(new_qty, ref4)), acct["initial_margin_bps"])
                           - _ceil_bps(abs(value_cents(pos_qty, cur_mark)), acct["initial_margin_bps"])) + fee
                if need > st.excess:
                    return insert("rejected", f"保证金不足（需要 ${need / 100:,.2f}，可用 ${max(st.excess, 0) / 100:,.2f}）")
                if otype == "limit":
                    reserved = need

        if reserved:
            conn.execute("UPDATE accounts SET reserved_cents = reserved_cents + ? WHERE id = ?", (reserved, account_id))
        order_id = insert("open", reserved=reserved)
        if otype == "market":
            _fill(conn, order_id, price4, quote.as_of, now)
        elif tradable:
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


# --- margin enforcement and financing --------------------------------------------

def enforce_maintenance(conn, account_id: int, quotes: dict[str, Quote], now: datetime) -> dict | None:
    """Margin call: if equity < maintenance, cancel open orders and force-close positions worst-first.

    Only positions with a tradable quote can be closed; the rest wait for the next check.
    Returns a summary when anything happened, else None. Equity <= 0 afterwards marks the account bust.
    """
    with db.tx(conn):
        acct = get_account(conn, account_id)
        if acct["account_type"] != "margin" or acct["status"] != "active":
            return None
        st = margin_state(conn, acct, quotes)
        if st.missing or st.equity >= st.maint_req:
            return None
        cancelled = 0
        for order in open_orders(conn, account_id):
            _close_open(conn, order, "cancelled", "保证金不足，系统撤单", now)
            cancelled += 1

        def unrealized(p):
            q = quotes[p["symbol"]]
            v = value_cents(p["qty"], q.price4)
            return v - p["cost_cents"] if p["qty"] > 0 else v + p["cost_cents"]

        closed = []
        for p in sorted(_positions(conn, account_id), key=unrealized):
            q = quotes[p["symbol"]]
            if not q.tradable:
                continue
            side = "sell" if p["qty"] > 0 else "buy"
            bps = acct["slippage_bps"]
            price4 = slip_down(q.price4, bps) if side == "sell" else slip_up(q.price4, bps)
            order_id = conn.execute(
                "INSERT INTO orders (account_id, symbol, side, type, qty, status, reason, trade_date, placed_at)"
                " VALUES (?, ?, ?, 'market', ?, 'open', '强制平仓：低于维持保证金', ?, ?)",
                (account_id, p["symbol"], side, abs(p["qty"]), now.date().isoformat(), iso(now))).lastrowid
            _fill(conn, order_id, price4, q.as_of, now)
            closed.append(p["symbol"])
            acct = get_account(conn, account_id)
            st = margin_state(conn, acct, quotes)
            if st.equity >= st.maint_req:
                break
        bust = st.equity <= 0
        if bust:
            conn.execute("UPDATE accounts SET status = 'bust' WHERE id = ?", (account_id,))
        return {"cancelled": cancelled, "closed": closed, "equity": st.equity / 100, "bust": bust}


def accrue_financing(conn, account_id: int, through: str, marks: dict[str, Quote | None], now: datetime) -> int:
    """Charge margin interest on borrowed cash and borrow fees on shorts, per calendar day up to `through`.

    Returns cents charged. The first call only sets the starting date.
    """
    with db.tx(conn):
        acct = get_account(conn, account_id)
        if acct["account_type"] != "margin" or acct["status"] != "active":
            return 0
        start = acct["accrued_through"]
        if start is None or start >= through:
            if start is None:
                conn.execute("UPDATE accounts SET accrued_through = ? WHERE id = ?", (through, account_id))
            return 0
        days = (date.fromisoformat(through) - date.fromisoformat(start)).days
        st = margin_state(conn, acct, marks)
        debit = max(0, -acct["cash_cents"])
        interest = -(-debit * acct["margin_rate_bps"] * days // (10000 * DAY_COUNT))
        borrow = -(-st.short_value * acct["borrow_rate_bps"] * days // (10000 * DAY_COUNT))
        conn.execute("UPDATE accounts SET cash_cents = cash_cents - ?, accrued_through = ? WHERE id = ?",
                     (interest + borrow, through, account_id))
        if interest:
            _ledger(conn, account_id, -interest, "margin_interest", None, now)
        if borrow:
            _ledger(conn, account_id, -borrow, "borrow_fee", None, now)
        return interest + borrow


# --- queries --------------------------------------------------------------------

def open_orders(conn, account_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM orders WHERE account_id = ? AND status = 'open' ORDER BY id",
                        (account_id,)).fetchall()


def held_symbols(conn, account_id: int) -> list[str]:
    return [p["symbol"] for p in _positions(conn, account_id)]


def portfolio(conn, account_id: int, marks: dict[str, Quote | None]) -> dict:
    acct = get_account(conn, account_id)
    margin = acct["account_type"] == "margin"
    st = margin_state(conn, acct, marks)
    realized = conn.execute("SELECT COALESCE(SUM(realized_cents), 0) r FROM positions WHERE account_id = ?",
                            (account_id,)).fetchone()["r"]
    positions = []
    for p in _positions(conn, account_id):
        mark = marks.get(p["symbol"])
        qty = p["qty"]
        value = value_cents(qty, mark.price4) if mark else None
        unrealized = None if value is None else (value - p["cost_cents"] if qty > 0 else value + p["cost_cents"])
        positions.append({
            "symbol": p["symbol"],
            "side": "long" if qty > 0 else "short",
            "qty": qty,
            "reserved_qty": p["reserved_qty"],
            "avg_cost": p["cost_cents"] / abs(qty) / 100,
            "cost": p["cost_cents"] / 100,
            "price": mark.price4 / 10000 if mark else None,
            "price_as_of": iso(mark.as_of) if mark else None,
            "market_value": value / 100 if value is not None else None,
            "unrealized": unrealized / 100 if unrealized is not None else None,
            "realized": p["realized_cents"] / 100,
            "liquidation_price": _liquidation_price(acct, st, qty, mark) if margin and mark and not st.missing else None,
        })
    out = {
        "account_id": account_id,
        "account_type": acct["account_type"],
        "cash": acct["cash_cents"] / 100,
        "reserved": acct["reserved_cents"] / 100,
        "available_cash": (acct["cash_cents"] - acct["reserved_cents"]) / 100,
        "market_value": (st.long_value - st.short_value) / 100,
        "equity": st.equity / 100,
        "initial": acct["initial_cents"] / 100,
        "total_return": (st.equity - acct["initial_cents"]) / acct["initial_cents"],
        "realized": realized / 100,
        "positions": positions,
        "unpriced_symbols": st.missing,
        "margin": None,
    }
    if margin:
        gross = st.long_value + st.short_value
        out["margin"] = {
            "leverage_max": 10000 / acct["initial_margin_bps"],
            "leverage": gross / st.equity if st.equity > 0 else None,
            "long_value": st.long_value / 100,
            "short_value": st.short_value / 100,
            "debit": max(0, -acct["cash_cents"]) / 100,
            "initial_requirement": st.initial_req / 100,
            "maintenance_requirement": st.maint_req / 100,
            "excess": st.excess / 100,
            "buying_power": max(0, st.excess) * 10000 / acct["initial_margin_bps"] / 100,
            "maintenance_excess": (st.equity - st.maint_req) / 100,
            "usage": st.maint_req / st.equity if st.equity > 0 else None,
            "margin_call": not st.missing and st.equity < st.maint_req,
            "margin_rate": acct["margin_rate_bps"] / 10000,
            "borrow_rate": acct["borrow_rate_bps"] / 10000,
        }
    return out


def _liquidation_price(acct, st: MarginState, qty: int, mark: Quote) -> float | None:
    """Price at which this position alone would push equity to the maintenance line (others held fixed)."""
    p0 = mark.price4 / 10000
    equity = st.equity / 100
    if qty > 0:
        m = acct["maint_long_bps"] / 10000
        others = st.maint_req / 100 - qty * p0 * m
        price = (others - equity + qty * p0) / (qty * (1 - m))
    else:
        s = -qty
        m = acct["maint_short_bps"] / 10000
        others = st.maint_req / 100 - s * p0 * m
        price = (equity + s * p0 - others) / (s * (1 + m))
    return round(price, 4) if price > 0 else None


# --- internals -----------------------------------------------------------------

def _positions(conn, account_id):
    return conn.execute("SELECT * FROM positions WHERE account_id = ? AND qty != 0 ORDER BY symbol",
                        (account_id,)).fetchall()


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
    """Book a fill: cash, position (open / add / reduce / close / flip), realized P&L, ledger."""
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    account_id, symbol, qty, side = order["account_id"], order["symbol"], order["qty"], order["side"]
    acct = get_account(conn, account_id)
    fee = acct["commission_cents"]
    amount = buy_cents(qty, price4) if side == "buy" else sell_cents(qty, price4)
    cash_delta = -(amount + fee) if side == "buy" else amount - fee

    pos = _position(conn, account_id, symbol)
    pos_qty, cost, realized = (pos["qty"], pos["cost_cents"], pos["realized_cents"]) if pos else (0, 0, 0)
    signed = qty if side == "buy" else -qty

    # Part of the fill that closes existing exposure (opposite sign), and the rest that opens/extends.
    close_n = min(qty, abs(pos_qty)) if pos_qty and (pos_qty > 0) != (signed > 0) else 0
    open_n = qty - close_n
    amt_close = amount * close_n // qty
    amt_open = amount - amt_close
    fee_close = fee if close_n else 0
    fee_open = fee - fee_close

    if close_n:
        cost_part = cost if close_n == abs(pos_qty) else (cost * close_n * 2 + abs(pos_qty)) // (2 * abs(pos_qty))
        if pos_qty > 0:   # selling longs
            realized += (amt_close - fee_close) - cost_part
        else:             # buying back shorts
            realized += cost_part - (amt_close + fee_close)
        cost -= cost_part
    if open_n:
        cost += amt_open + fee_open if side == "buy" else amt_open - fee_open
    new_qty = pos_qty + signed

    released_qty = qty if (acct["account_type"] == "cash" and side == "sell" and order["type"] == "limit") else 0
    conn.execute(
        "INSERT INTO positions (account_id, symbol, qty, reserved_qty, cost_cents, realized_cents)"
        " VALUES (?, ?, ?, 0, ?, ?) ON CONFLICT (account_id, symbol) DO UPDATE SET qty = excluded.qty,"
        " reserved_qty = reserved_qty - ?, cost_cents = excluded.cost_cents, realized_cents = excluded.realized_cents",
        (account_id, symbol, new_qty, cost if new_qty else 0, realized, released_qty))
    conn.execute("UPDATE accounts SET cash_cents = cash_cents + ?, reserved_cents = reserved_cents - ? WHERE id = ?",
                 (cash_delta, order["reserved_cents"], account_id))

    fill_id = conn.execute(
        "INSERT INTO fills (order_id, account_id, symbol, side, qty, price4, amount_cents, fees_cents,"
        " quote_as_of, executed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, account_id, symbol, side, qty, price4, amount, fee, iso(quote_as_of), iso(now))).lastrowid
    _ledger(conn, account_id, cash_delta, side, fill_id, now)
    conn.execute("UPDATE orders SET status = 'filled', fill_price4 = ?, closed_at = ? WHERE id = ?",
                 (price4, iso(now), order_id))


def _close_open(conn, order, status: str, reason: str | None, now: datetime) -> None:
    conn.execute("UPDATE accounts SET reserved_cents = reserved_cents - ? WHERE id = ?",
                 (order["reserved_cents"], order["account_id"]))
    if order["side"] == "sell" and get_account(conn, order["account_id"])["account_type"] == "cash":
        conn.execute("UPDATE positions SET reserved_qty = reserved_qty - ? WHERE account_id = ? AND symbol = ?",
                     (order["qty"], order["account_id"], order["symbol"]))
    conn.execute("UPDATE orders SET status = ?, reason = ?, closed_at = ? WHERE id = ?",
                 (status, reason, iso(now), order["id"]))


def _ledger(conn, account_id, amount, category, fill_id, now) -> None:
    balance = conn.execute("SELECT cash_cents FROM accounts WHERE id = ?", (account_id,)).fetchone()["cash_cents"]
    conn.execute("INSERT INTO cash_ledger (account_id, amount_cents, balance_cents, category, fill_id, created_at)"
                 " VALUES (?, ?, ?, ?, ?, ?)", (account_id, amount, balance, category, fill_id, iso(now)))
