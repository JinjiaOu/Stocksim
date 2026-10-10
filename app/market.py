"""Market data access (yfinance) and live-session rules."""
import logging
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from .models import Quote, to_price4

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
OPEN_T, CLOSE_T = dtime(9, 30), dtime(16, 0)
QUOTE_TTL = 30          # seconds a fetched quote is reused
STALE_SECONDS = 180     # quotes older than this are not tradable
JUMP_LIMIT = 0.30       # a >30% move between consecutive quotes pauses matching for that quote
US_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS", "NAS", "NYS", "CXI"}


class MarketDataProvider:
    """Interface; YFinanceProvider is the only implementation for now."""

    def quotes(self, symbols: list[str]) -> dict[str, Quote]: ...
    def intraday_bars(self, symbol: str) -> list[dict]: ...
    def daily_bars(self, symbol: str, period: str) -> pd.DataFrame | None: ...
    def search(self, q: str) -> list[dict]: ...


class YFinanceProvider(MarketDataProvider):
    def __init__(self):
        self._lock = threading.Lock()
        self._quotes: dict[str, tuple[float, Quote | None]] = {}
        self._bars: dict[tuple, tuple[float, object]] = {}

    def quotes(self, symbols):
        now = time.monotonic()
        with self._lock:
            need = [s for s in symbols if s not in self._quotes or now - self._quotes[s][0] > QUOTE_TTL]
        if need:
            fetched = self._fetch_quotes(need, "1d")
            missing = [s for s in need if s not in fetched]
            if missing:  # e.g. early morning before the first bar: fall back to the last session
                fetched |= self._fetch_quotes(missing, "5d")
            with self._lock:
                for s in need:
                    self._quotes[s] = (now, fetched.get(s))
        with self._lock:
            return {s: q for s in symbols if (q := self._quotes[s][1]) is not None}

    def _fetch_quotes(self, symbols, period) -> dict[str, Quote]:
        try:
            df = yf.download(symbols, period=period, interval="1m", group_by="ticker", auto_adjust=False,
                             progress=False, threads=True)
        except Exception:
            log.exception("quote download failed for %s", symbols)
            return {}
        out = {}
        for s in symbols:
            try:
                sub = df[s] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                continue
            # A 1m bar is stamped with its start time; the close price is known at its end.
            as_of = sub.index[-1].to_pydatetime().astimezone(timezone.utc) + timedelta(minutes=1)
            out[s] = Quote(s, to_price4(sub["Close"].iloc[-1]), as_of, "yfinance:1m")
        return out

    def intraday_bars(self, symbol):
        def fetch():
            df = yf.Ticker(symbol).history(period="1d", interval="1m", auto_adjust=False)
            if df.empty:
                df = yf.Ticker(symbol).history(period="5d", interval="1m", auto_adjust=False)
                if not df.empty:
                    df = df[df.index.date == df.index[-1].date()]
            return _bars_json(df)
        return self._cached(("1m", symbol), 30, fetch)

    def daily_bars(self, symbol, period="1y"):
        """Split/dividend-adjusted daily bars, NaN rows dropped. None on failure."""
        def fetch():
            try:
                df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True, actions=False)
            except Exception:
                log.exception("daily download failed for %s", symbol)
                return None
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            return None if df.empty else df
        return self._cached(("1d", symbol, period), 900, fetch)

    def search(self, q):
        def fetch():
            try:
                quotes = yf.Search(q, max_results=15, news_count=0).quotes
            except Exception:
                log.exception("search failed for %r", q)
                return []
            return [{"symbol": r["symbol"], "name": r.get("shortname") or r.get("longname") or "",
                     "exchange": r.get("exchDisp", ""), "type": r.get("quoteType", "")}
                    for r in quotes
                    if r.get("quoteType") in ("EQUITY", "ETF") and r.get("exchange") in US_EXCHANGES]
        return self._cached(("search", q.lower()), 3600, fetch)

    def _cached(self, key, ttl, fn):
        now = time.monotonic()
        with self._lock:
            hit = self._bars.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = fn()
        with self._lock:
            self._bars[key] = (now, value)
        return value


def _bars_json(df: pd.DataFrame) -> list[dict]:
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return [{"time": int(ts.timestamp()), "open": float(r.Open), "high": float(r.High), "low": float(r.Low),
             "close": float(r.Close), "volume": int(r.Volume)} for ts, r in zip(df.index, df.itertuples())]


# --- live session rules ---------------------------------------------------------

def in_regular_hours(now: datetime) -> bool:
    et = now.astimezone(ET)
    return et.weekday() < 5 and OPEN_T <= et.time() < CLOSE_T


def session_date(now: datetime) -> date:
    """The trading day a DAY order placed at `now` belongs to (next weekday once today's session is over).

    Exchange holidays are not modelled: an order placed the evening before a holiday expires unfilled.
    """
    et = now.astimezone(ET)
    d = et.date()
    if et.weekday() >= 5 or et.time() >= CLOSE_T:
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
    return d


class LiveMarket:
    """Turns raw provider quotes into tradable/untradable quotes for the live account."""

    BENCH = "SPY"

    def __init__(self, provider: MarketDataProvider):
        self.provider = provider
        self._seen: dict[str, tuple[datetime, int, bool]] = {}
        self._lock = threading.Lock()

    def status(self, now: datetime) -> dict:
        is_open, reason = self._open(now, self.provider.quotes([self.BENCH]).get(self.BENCH))
        return {"mode": "live", "open": is_open, "reason": reason, "now": now.isoformat(timespec="seconds"),
                "session_date": session_date(now).isoformat()}

    def quotes(self, symbols: list[str], now: datetime) -> dict[str, Quote]:
        raw = self.provider.quotes(sorted(set(symbols) | {self.BENCH}))
        is_open, closed_reason = self._open(now, raw.get(self.BENCH))
        out = {}
        for s in symbols:
            q = raw.get(s)
            if q is None:
                continue
            jumped = self._jumped(q)
            if not is_open:
                reason = closed_reason
            elif (now - q.as_of).total_seconds() > STALE_SECONDS:
                reason = "行情过期"
            elif jumped:
                reason = "价格异常跳变，暂停撮合"
            else:
                reason = None
            out[s] = Quote(q.symbol, q.price4, q.as_of, q.source, tradable=reason is None, reason=reason)
        return out

    def _open(self, now, bench: Quote | None) -> tuple[bool, str | None]:
        if not in_regular_hours(now):
            return False, "市场未开盘（常规时段 09:30–16:00 ET）"
        # Weekday within hours but no fresh SPY prints after the first few minutes: holiday or data outage.
        et = now.astimezone(ET)
        if et.time() >= dtime(9, 35) and (bench is None or (now - bench.as_of).total_seconds() > 300):
            return False, "无最新行情（可能为休市日或数据源异常）"
        return True, None

    def _jumped(self, q: Quote) -> bool:
        with self._lock:
            prev = self._seen.get(q.symbol)
            if prev and prev[0] == q.as_of:
                return prev[2]
            jumped = bool(prev) and abs(q.price4 / prev[1] - 1) > JUMP_LIMIT
            self._seen[q.symbol] = (q.as_of, q.price4, jumped)
            return jumped
