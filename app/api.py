import re
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import db, replay, trading
from .market import ET, LiveMarket, YFinanceProvider, session_date
from .models import iso, to_price4, utcnow
from .trading import TradeError

router = APIRouter(prefix="/api")
provider = YFinanceProvider()
live = LiveMarket(provider)

SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}(=X)?$")  # =X: Yahoo currency pairs


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def symbol_of(raw: str) -> str:
    s = raw.strip().upper()
    if not SYMBOL_RE.match(s):
        raise HTTPException(400, "无效的代码")
    return s


def account_of(conn, account_id: int | None):
    try:
        return trading.get_account(conn, account_id if account_id is not None else db.live_account_id(conn))
    except TradeError as e:
        raise HTTPException(e.status, str(e))


def quote_json(q, now=None, prev=None):
    if q is None:
        return None
    out = {"symbol": q.symbol, "price": q.price4 / 1e4, "as_of": iso(q.as_of), "source": q.source,
           "tradable": q.tradable, "reason": q.reason, "prev_close": prev}
    if now is not None:
        out["age_seconds"] = int((now - q.as_of).total_seconds())
    return out


def prev_close(conn, acct, q) -> float | None:
    """Close of the trading day before the quote's day, for daily change display."""
    if q is None:
        return None
    if acct["mode"] == "replay":
        row = conn.execute("SELECT close4 FROM daily_bars WHERE symbol = ? AND date < ? ORDER BY date DESC LIMIT 1",
                           (q.symbol, acct["sim_date"])).fetchone()
        return row["close4"] / 1e4 if row else None
    closes = provider.daily_closes([q.symbol])[q.symbol]  # served from cache when pre-fetched in batch
    day = q.as_of.astimezone(ET).date()
    before = [d for d in closes if d < day]
    return closes[max(before)] if before else None


def live_quotes_json(conn, acct, symbols):
    now = utcnow()
    quotes = live.quotes(symbols, now) if symbols else {}
    provider.daily_closes(symbols)
    return {s: quote_json(q, now, prev_close(conn, acct, q)) for s in symbols if (q := quotes.get(s))}


def replay_ready(conn, acct, symbol):
    if not replay.ensure_daily(conn, provider, symbol, acct["sim_date"]):
        raise HTTPException(404, f"找不到 {symbol} 的历史数据")


def account_json(a):
    return {k: a[k] for k in ("id", "mode", "account_type", "market", "status", "name", "start_date", "sim_date", "sim_phase",
                              "created_at")} | {
        "initial": a["initial_cents"] / 100, "slippage_bps": a["slippage_bps"], "commission": a["commission_cents"] / 100,
        "leverage": round(10000 / a["initial_margin_bps"], 2), "maint_long_bps": a["maint_long_bps"],
        "maint_short_bps": a["maint_short_bps"], "initial_margin_bps": a["initial_margin_bps"]}


def et_today() -> str:
    return utcnow().astimezone(ET).date().isoformat()


# --- accounts -------------------------------------------------------------------

@router.get("/accounts")
def list_accounts(conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM accounts WHERE status != 'archived' ORDER BY mode, id").fetchall()
    return [account_json(a) for a in rows]


class NewAccount(BaseModel):
    mode: Literal["live", "replay"] = "replay"
    account_type: Literal["cash", "margin"] = "cash"
    market: Literal["us", "fx"] = "us"
    leverage: Literal[1, 2, 4, 10, 20] | None = None  # us margin: 1/2/4 (default 2); fx: 10/20 (default 20)
    start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    initial: Decimal = Field(default=Decimal(100_000), ge=Decimal(1000), le=Decimal(1_000_000_000))


@router.post("/accounts")
def new_account(body: NewAccount, conn=Depends(get_conn)):
    initial = int(body.initial * 100)
    if body.market == "fx":
        account_type, leverage = "margin", body.leverage or 20
        if leverage not in trading.FX_LEVERAGES:
            raise HTTPException(422, "外汇账户杠杆只能是 10x 或 20x")
    else:
        account_type = body.account_type
        leverage = (body.leverage or 2) if account_type == "margin" else 1
        if leverage not in (1, 2, 4):
            raise HTTPException(422, "股票保证金账户杠杆只能是 1x、2x 或 4x")
    try:
        if body.mode == "replay":
            account_id = replay.new_game(conn, provider, initial, body.start_date, account_type=account_type,
                                         leverage=leverage, market=body.market)
        else:
            if body.market == "fx":
                label = f"实时 · 外汇 {leverage}x"
            else:
                label = "实时 · 现金" if account_type == "cash" else f"实时 · 保证金 {leverage}x"
            account_id = trading.create_account(conn, "live", label, initial, utcnow(), account_type=account_type,
                                                leverage=leverage, accrued_through=et_today(), market=body.market)
    except TradeError as e:
        raise HTTPException(e.status, str(e))
    return account_json(trading.get_account(conn, account_id))


@router.post("/replay/new")
def new_replay(body: NewAccount, conn=Depends(get_conn)):
    return new_account(body.model_copy(update={"mode": "replay"}), conn)


@router.post("/accounts/{account_id}/archive")
def archive_account(account_id: int, conn=Depends(get_conn)):
    acct = account_of(conn, account_id)
    if acct["id"] == db.live_account_id(conn):
        raise HTTPException(400, "默认实时账户不能归档，可以重置")
    with db.tx(conn):
        for o in trading.open_orders(conn, acct["id"]):
            trading.cancel_order(conn, acct["id"], o["id"], utcnow())
        conn.execute("UPDATE accounts SET status = 'archived' WHERE id = ?", (acct["id"],))
    return {"ok": True}


@router.post("/account/reset")
def reset_account(account_id: int | None = None, conn=Depends(get_conn)):
    acct = account_of(conn, account_id)
    if acct["mode"] != "live":
        raise HTTPException(400, "回放存档不能重置，请新建一局")
    trading.reset_account(conn, acct["id"], utcnow(), et_today())
    return {"ok": True}


class Advance(BaseModel):
    account_id: int
    step: Literal["half", "day", "week", "month"] = "half"


@router.post("/replay/advance")
def advance_replay(body: Advance, conn=Depends(get_conn)):
    acct = account_of(conn, body.account_id)
    if acct["mode"] == "replay":  # make sure every symbol we might need to match has history cached
        for s in {o["symbol"] for o in trading.open_orders(conn, acct["id"])}:
            replay.ensure_daily(conn, provider, s)
    try:
        return replay.advance(conn, acct["id"], replay.STEPS[body.step])
    except TradeError as e:
        raise HTTPException(e.status, str(e))


# --- market ---------------------------------------------------------------------

@router.get("/market/status")
def market_status(account_id: int | None = None, conn=Depends(get_conn)):
    acct = account_of(conn, account_id)
    if acct["mode"] == "live":
        return live.status(utcnow())
    return {"mode": "replay", "open": acct["status"] == "active", "status": acct["status"],
            "reason": None if acct["status"] == "active" else "回放已结束",
            "now": iso(replay.now_of(acct)), "sim_date": acct["sim_date"], "sim_phase": acct["sim_phase"],
            "session_date": replay.session_date(conn, acct)}


@router.get("/market/search")
def search(q: str = Query(min_length=1, max_length=40)):
    return provider.search(q.strip())


@router.get("/market/quotes")
def get_quotes(symbols: str = Query(max_length=2000), account_id: int | None = None, conn=Depends(get_conn)):
    """Batch quotes for list views. Replay returns only symbols already cached (no bulk history downloads)."""
    syms = list(dict.fromkeys(symbol_of(s) for s in symbols.split(",") if s.strip()))[:150]
    acct = account_of(conn, account_id)
    if acct["mode"] == "live":
        return live_quotes_json(conn, acct, syms)
    cached = {r["symbol"] for r in conn.execute("SELECT symbol FROM bar_meta")}
    return {s: quote_json(q, None, prev_close(conn, acct, q))
            for s in syms if s in cached and (q := replay.quote(conn, acct, s))}


@router.get("/market/quote/{symbol}")
def get_quote(symbol: str, account_id: int | None = None, conn=Depends(get_conn)):
    s = symbol_of(symbol)
    acct = account_of(conn, account_id)
    if acct["mode"] == "live":
        now = utcnow()
        q = live.quotes([s], now).get(s)
    else:
        replay_ready(conn, acct, s)
        now, q = None, replay.quote(conn, acct, s)
        if q is None:
            raise HTTPException(404, f"{s} 在 {acct['sim_date']} 还没有交易数据（可能尚未上市）")
    if q is None:
        raise HTTPException(404, f"暂无 {s} 的行情")
    return quote_json(q, now, prev_close(conn, acct, q))


@router.get("/market/bars/{symbol}")
def get_bars(symbol: str, account_id: int | None = None, range: Literal["1d", "1y", "5y"] = "1y",
             conn=Depends(get_conn)):
    s = symbol_of(symbol)
    acct = account_of(conn, account_id)
    if acct["mode"] == "replay":
        replay_ready(conn, acct, s)
        return replay.bars(conn, acct, s, 1260 if range == "5y" else 252)
    if range == "1d":
        return provider.intraday_bars(s)
    df = provider.daily_bars(s, range)
    if df is None:
        return []
    return [{"time": ts.date().isoformat(), "open": float(r.Open), "high": float(r.High), "low": float(r.Low),
             "close": float(r.Close), "volume": int(r.Volume)} for ts, r in zip(df.index, df.itertuples())]


# --- trading --------------------------------------------------------------------

class OrderIn(BaseModel):
    account_id: int | None = None
    symbol: str
    side: Literal["buy", "sell"]
    type: Literal["market", "limit"]
    qty: int = Field(gt=0, le=10_000_000)
    limit_price: Decimal | None = Field(default=None, gt=0, le=Decimal(1_000_000), decimal_places=4)


def order_json(o):
    return {"id": o["id"], "symbol": o["symbol"], "side": o["side"], "type": o["type"], "qty": o["qty"],
            "limit_price": o["limit_price4"] / 1e4 if o["limit_price4"] else None, "status": o["status"],
            "reason": o["reason"], "reserved": o["reserved_cents"] / 100,
            "fill_price": o["fill_price4"] / 1e4 if o["fill_price4"] else None,
            "trade_date": o["trade_date"], "placed_at": o["placed_at"], "closed_at": o["closed_at"]}


def marks_for(conn, acct, symbols) -> dict:
    """Latest price per symbol (tradable or not) for valuation and margin sizing."""
    if not symbols:
        return {}
    if acct["mode"] == "live":
        return live.quotes(list(symbols), utcnow())
    return {s: replay.mark(conn, acct, s) for s in symbols}


@router.post("/orders")
def place_order(body: OrderIn, conn=Depends(get_conn)):
    s = symbol_of(body.symbol)
    acct = account_of(conn, body.account_id)
    if acct["mode"] == "live":
        now = utcnow()
        q = live.quotes([s], now).get(s)
        trade_date = session_date(now).isoformat()
    else:
        replay_ready(conn, acct, s)
        now, q = replay.now_of(acct), replay.quote(conn, acct, s)
        trade_date = replay.session_date(conn, acct)
    marks = marks_for(conn, acct, set(trading.held_symbols(conn, acct["id"])) | {s}) \
        if acct["account_type"] == "margin" else None
    limit4 = to_price4(body.limit_price) if body.limit_price is not None else None
    try:
        order_id = trading.place_order(conn, acct["id"], s, body.side, body.type, body.qty, limit4, q, trade_date, now,
                                       marks)
    except TradeError as e:
        raise HTTPException(e.status, str(e))
    return order_json(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, account_id: int | None = None, conn=Depends(get_conn)):
    acct = account_of(conn, account_id)
    now = utcnow() if acct["mode"] == "live" else replay.now_of(acct)
    try:
        trading.cancel_order(conn, acct["id"], order_id, now)
    except TradeError as e:
        raise HTTPException(e.status, str(e))
    return order_json(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())


@router.get("/orders")
def list_orders(account_id: int | None = None, status: str | None = None, limit: int = Query(200, le=1000),
                conn=Depends(get_conn)):
    acct = account_of(conn, account_id)
    sql, args = "SELECT * FROM orders WHERE account_id = ?", [acct["id"]]
    if status:
        sql += " AND status = ?"
        args.append(status)
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return [order_json(o) for o in rows]


@router.get("/fills")
def list_fills(account_id: int | None = None, limit: int = Query(200, le=1000), conn=Depends(get_conn)):
    acct = account_of(conn, account_id)
    rows = conn.execute("SELECT * FROM fills WHERE account_id = ? ORDER BY id DESC LIMIT ?", (acct["id"], limit))
    return [{"id": f["id"], "order_id": f["order_id"], "symbol": f["symbol"], "side": f["side"], "qty": f["qty"],
             "price": f["price4"] / 1e4, "amount": f["amount_cents"] / 100, "fees": f["fees_cents"] / 100,
             "quote_as_of": f["quote_as_of"], "executed_at": f["executed_at"], "estimated": bool(f["estimated"])}
            for f in rows]


@router.get("/portfolio")
def get_portfolio(account_id: int | None = None, conn=Depends(get_conn)):
    acct = account_of(conn, account_id)
    out = trading.portfolio(conn, acct["id"], marks_for(conn, acct, trading.held_symbols(conn, acct["id"])))
    out["benchmark_return"] = None
    if acct["mode"] == "replay":  # buy-and-hold SPY from the game's first open
        start = conn.execute("SELECT open4 FROM daily_bars WHERE symbol = ? AND date = ?",
                             (replay.CALENDAR, acct["start_date"])).fetchone()
        now_mark = replay.mark(conn, acct, replay.CALENDAR)
        if start and now_mark:
            out["benchmark_return"] = now_mark.price4 / start["open4"] - 1
    return out


# --- watchlist ------------------------------------------------------------------

@router.get("/watchlist")
def get_watchlist(account_id: int | None = None, conn=Depends(get_conn)):
    acct = account_of(conn, account_id)
    symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM watchlist WHERE account_id = ? ORDER BY added_at",
                                                 (acct["id"],))]
    if acct["mode"] == "live":
        quotes = live_quotes_json(conn, acct, symbols)
    else:
        quotes = {s: quote_json(q, None, prev_close(conn, acct, q)) for s in symbols if (q := replay.quote(conn, acct, s))}
    return [{"symbol": s, "quote": quotes.get(s)} for s in symbols]


@router.post("/watchlist/{symbol}")
def add_watch(symbol: str, account_id: int | None = None, conn=Depends(get_conn)):
    s = symbol_of(symbol)
    acct = account_of(conn, account_id)
    if acct["mode"] == "replay":
        replay_ready(conn, acct, s)
    with db.tx(conn):
        conn.execute("INSERT OR IGNORE INTO watchlist VALUES (?, ?, ?)", (acct["id"], s, iso(utcnow())))
    return {"ok": True}


@router.delete("/watchlist/{symbol}")
def remove_watch(symbol: str, account_id: int | None = None, conn=Depends(get_conn)):
    acct = account_of(conn, account_id)
    with db.tx(conn):
        conn.execute("DELETE FROM watchlist WHERE account_id = ? AND symbol = ?", (acct["id"], symbol_of(symbol)))
    return {"ok": True}
