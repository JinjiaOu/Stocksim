"""P0: verify yfinance data availability, timestamps and failure behavior."""
import time
from datetime import datetime, timezone

import yfinance as yf

SYMBOLS = ["AAPL", "NVDA", "SPY"]


def show(label, fn):
    t0 = time.perf_counter()
    try:
        out = fn()
        print(f"[ok  {time.perf_counter() - t0:5.2f}s] {label}: {out}")
    except Exception as e:
        print(f"[ERR {time.perf_counter() - t0:5.2f}s] {label}: {type(e).__name__}: {e}")


def fast_quote(sym):
    fi = yf.Ticker(sym).fast_info
    return {"last": fi.last_price, "prev_close": fi.previous_close, "currency": fi.currency, "tz": fi.timezone}


def bars(sym, period, interval, prepost=False):
    df = yf.Ticker(sym).history(period=period, interval=interval, prepost=prepost, auto_adjust=False, actions=True)
    if df.empty:
        return "EMPTY"
    last = df.index[-1]
    return (f"rows={len(df)} first={df.index[0]} last={last} "
            f"age={(datetime.now(timezone.utc) - last.to_pydatetime()).total_seconds() / 60:.1f}min "
            f"close={df['Close'].iloc[-1]:.2f} dup_ts={df.index.duplicated().sum()}")


print("now UTC:", datetime.now(timezone.utc).isoformat())
for s in SYMBOLS:
    show(f"{s} fast_info", lambda s=s: fast_quote(s))
    show(f"{s} 1d/1m", lambda s=s: bars(s, "1d", "1m"))
    show(f"{s} 1d/1m prepost", lambda s=s: bars(s, "1d", "1m", True))
    show(f"{s} 1y/1d", lambda s=s: bars(s, "1y", "1d"))

show("1m window 8d", lambda: bars("AAPL", "8d", "1m"))
show("1m window 30d", lambda: bars("AAPL", "30d", "1m"))
show("5m window 60d", lambda: bars("AAPL", "60d", "5m"))
show("batch download", lambda: yf.download(SYMBOLS, period="1d", interval="1m", group_by="ticker",
                                            auto_adjust=False, progress=False).shape)
show("search 'nvidia'", lambda: [q.get("symbol") for q in yf.Search("nvidia", max_results=5).quotes])
show("bad symbol", lambda: bars("ZZZZNOTREAL", "5d", "1d"))
show("AAPL splits", lambda: yf.Ticker("AAPL").splits.tail(3).to_dict())
