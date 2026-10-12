import asyncio
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db, trading
from .api import live_of, router
from .market import ET
from .models import utcnow

log = logging.getLogger("stocksim")
LOOP_SECONDS = 30
WEB_DIR = (Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent) / "web"
IDLE_EXIT_SECONDS = 180  # browsers throttle background tabs to ~1 timer/min, so stay well above that
last_ping = time.monotonic()
watch_idle = False  # set by the exe launcher: quit when no browser tab has pinged for a while


async def idle_watchdog() -> None:
    while True:
        await asyncio.sleep(5)
        if watch_idle and time.monotonic() - last_ping > IDLE_EXIT_SECONDS:
            log.info("no browser connected, exiting")
            os._exit(0)


def live_tick() -> None:
    """For each live account: expire stale DAY orders, match open orders, accrue financing, enforce margin."""
    conn = db.connect()
    try:
        now = utcnow()
        today = now.astimezone(ET).date().isoformat()
        for acct in conn.execute("SELECT * FROM accounts WHERE mode = 'live' AND status = 'active'").fetchall():
            account_id = acct["id"]
            market = live_of(acct)
            trading.expire_orders(conn, account_id, market.session_date(now).isoformat(), now)
            held = trading.held_symbols(conn, account_id)
            symbols = sorted({o["symbol"] for o in trading.open_orders(conn, account_id)} | set(held))
            if not symbols:
                continue
            quotes = market.quotes(symbols, now)
            if any(q.tradable for q in quotes.values()):
                n = trading.match_open_orders(conn, account_id, quotes, now)
                if n:
                    log.info("account %d: filled %d live order(s)", account_id, n)
            if acct["account_type"] == "margin":
                trading.accrue_financing(conn, account_id, today, quotes, now)
                result = trading.enforce_maintenance(conn, account_id, quotes, now)
                if result:
                    log.warning("account %d margin call: %s", account_id, result)
    finally:
        conn.close()


async def matcher_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(live_tick)
        except Exception:
            log.exception("live tick failed")
        await asyncio.sleep(LOOP_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init(conn)
    conn.close()
    task = asyncio.create_task(matcher_loop())
    watchdog = asyncio.create_task(idle_watchdog())
    yield
    task.cancel()
    watchdog.cancel()


app = FastAPI(title="StockSim", lifespan=lifespan)
app.include_router(router)


@app.post("/api/ping", include_in_schema=False)
def ping():
    global last_ping
    last_ping = time.monotonic()
    return {"ok": True, "exe": watch_idle}


@app.post("/api/quit", include_in_schema=False)
def quit_app():
    if watch_idle:  # only the packaged exe; never kill a dev server from the page
        threading.Timer(0.3, os._exit, (0,)).start()
    return {"ok": watch_idle}

if WEB_DIR.is_dir():
    app.mount("/web", StaticFiles(directory=WEB_DIR, html=True), name="web")

    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse("/web/")
