import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db, trading
from .api import live, router
from .market import session_date
from .models import utcnow

log = logging.getLogger("stocksim")
LOOP_SECONDS = 30
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def live_tick() -> None:
    """Expire stale DAY orders and match open live orders while the market is open."""
    conn = db.connect()
    try:
        account_id = db.live_account_id(conn)
        now = utcnow()
        trading.expire_orders(conn, account_id, session_date(now).isoformat(), now)
        symbols = sorted({o["symbol"] for o in trading.open_orders(conn, account_id)})
        if symbols:
            quotes = live.quotes(symbols, now)
            if any(q.tradable for q in quotes.values()):
                n = trading.match_open_orders(conn, account_id, quotes, now)
                if n:
                    log.info("filled %d live order(s)", n)
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
    yield
    task.cancel()


app = FastAPI(title="StockSim", lifespan=lifespan)
app.include_router(router)

if WEB_DIR.is_dir():
    app.mount("/web", StaticFiles(directory=WEB_DIR, html=True), name="web")

    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse("/web/")
