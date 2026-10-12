"""D1: probe yfinance FX data for the four USD pairs, for Claude's backend design.

Run:  F:\\code\\stocksim\\.venv\\Scripts\\python.exe scripts\\fx_probe.py
"""
import tempfile
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

# yfinance keeps sqlite caches (tz / cookie / isin) under the user cache dir, which the
# DSH sandbox may mount read-only; point them at the writable system temp dir instead.
yf.set_tz_cache_location(tempfile.gettempdir())

PAIRS = ["EURUSD=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X"]
ET = ZoneInfo("America/New_York")


def show(label, fn):
    t0 = time.perf_counter()
    try:
        out = fn()
        print(f"[ok  {time.perf_counter() - t0:5.2f}s] {label}: {out}")
    except Exception as e:
        print(f"[ERR {time.perf_counter() - t0:5.2f}s] {label}: {type(e).__name__}: {e}")


def bars(sym, period, interval):
    df = yf.Ticker(sym).history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        return "EMPTY"
    idx = df.index
    tz = getattr(idx, "tz", None)
    vols = df["Volume"]
    weekend = int((idx.weekday >= 5).sum())
    last_et = idx[-1].to_pydatetime().astimezone(ET)
    return (f"rows={len(df)} tz={tz} first={idx[0]} last={idx[-1]} "
            f"last_et={last_et:%a %H:%M} weekend_rows={weekend} "
            f"vol_zero={int((vols == 0).all())} vol_unique={sorted(set(vols.tolist()))[:6]}")


def search_fields(q):
    quotes = yf.Search(q, max_results=15, news_count=0).quotes
    lines = []
    for r in quotes:
        lines.append(f"symbol={r.get('symbol')!r} quoteType={r.get('quoteType')!r} "
                     f"exchange={r.get('exchange')!r} exchDisp={r.get('exchDisp')!r} "
                     f"shortname={r.get('shortname')!r}")
    return "\n    ".join(lines)


now = datetime.now(timezone.utc)
print(f"now UTC: {now.isoformat()}  ET: {now.astimezone(ET).isoformat()}")

print("\n=== 1d bars (period=1mo) ===")
for s in PAIRS:
    show(f"{s} 1mo/1d", lambda s=s: bars(s, "1mo", "1d"))

print("\n=== 1m bars (period=5d) ===")
for s in PAIRS:
    show(f"{s} 5d/1m", lambda s=s: bars(s, "5d", "1m"))

print("\n=== 1m bars (period=1d) — the window market.py uses for live quotes ===")
for s in PAIRS:
    show(f"{s} 1d/1m", lambda s=s: bars(s, "1d", "1m"))

print("\n=== batch yf.download (the path market.py uses for quotes) ===")
show("batch 1d/1m", lambda: yf.download(PAIRS, period="1d", interval="1m", group_by="ticker",
                                        auto_adjust=False, progress=False).shape)
show("batch 5d/1d", lambda: yf.download(PAIRS, period="5d", interval="1d", group_by="ticker",
                                        auto_adjust=False, progress=False).shape)

print("\n=== yf.Search('EUR') fields ===")
show("search 'EUR'", lambda: search_fields("EUR"))
