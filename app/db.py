import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("STOCKSIM_DB") or Path(__file__).resolve().parent.parent / "data" / "stocksim.db")
LIVE_INITIAL_CENTS = 100_000_00

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id               INTEGER PRIMARY KEY,
    mode             TEXT NOT NULL CHECK (mode IN ('live', 'replay')),
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'finished', 'archived')),
    name             TEXT NOT NULL,
    initial_cents    INTEGER NOT NULL CHECK (initial_cents > 0),
    cash_cents       INTEGER NOT NULL,
    reserved_cents   INTEGER NOT NULL DEFAULT 0,
    slippage_bps     INTEGER NOT NULL DEFAULT 5 CHECK (slippage_bps BETWEEN 0 AND 500),
    commission_cents INTEGER NOT NULL DEFAULT 0 CHECK (commission_cents >= 0),
    start_date       TEXT,
    sim_date         TEXT,
    sim_phase        TEXT CHECK (sim_phase IN ('open', 'close')),
    created_at       TEXT NOT NULL,
    CHECK (cash_cents >= 0),
    CHECK (reserved_cents >= 0 AND reserved_cents <= cash_cents)
);

CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    type           TEXT NOT NULL CHECK (type IN ('market', 'limit')),
    qty            INTEGER NOT NULL CHECK (qty > 0),
    limit_price4   INTEGER CHECK (limit_price4 > 0),
    status         TEXT NOT NULL CHECK (status IN ('open', 'filled', 'cancelled', 'expired', 'rejected')),
    reason         TEXT,
    reserved_cents INTEGER NOT NULL DEFAULT 0 CHECK (reserved_cents >= 0),
    fill_price4    INTEGER,
    trade_date     TEXT NOT NULL,
    placed_at      TEXT NOT NULL,
    closed_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_account_status ON orders(account_id, status);

CREATE TABLE IF NOT EXISTS fills (
    id           INTEGER PRIMARY KEY,
    order_id     INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    qty          INTEGER NOT NULL,
    price4       INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    fees_cents   INTEGER NOT NULL,
    quote_as_of  TEXT NOT NULL,
    executed_at  TEXT NOT NULL,
    estimated    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS positions (
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    symbol         TEXT NOT NULL,
    qty            INTEGER NOT NULL CHECK (qty >= 0),
    reserved_qty   INTEGER NOT NULL DEFAULT 0,
    cost_cents     INTEGER NOT NULL DEFAULT 0 CHECK (cost_cents >= 0),
    realized_cents INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, symbol),
    CHECK (reserved_qty >= 0 AND reserved_qty <= qty)
);

CREATE TABLE IF NOT EXISTS cash_ledger (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    amount_cents  INTEGER NOT NULL,
    balance_cents INTEGER NOT NULL,
    category      TEXT NOT NULL,
    fill_id       INTEGER REFERENCES fills(id),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    symbol     TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);

CREATE TABLE IF NOT EXISTS daily_bars (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    open4  INTEGER NOT NULL,
    high4  INTEGER NOT NULL,
    low4   INTEGER NOT NULL,
    close4 INTEGER NOT NULL,
    volume INTEGER NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS bar_meta (
    symbol     TEXT PRIMARY KEY,
    first_date TEXT NOT NULL,
    last_date  TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """Write transaction; nested use joins the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init(conn: sqlite3.Connection) -> None:
    from .models import utcnow
    from .trading import create_account

    conn.executescript(SCHEMA)
    with tx(conn):
        if not conn.execute("SELECT 1 FROM accounts WHERE mode = 'live'").fetchone():
            create_account(conn, "live", "实时账户", LIVE_INITIAL_CENTS, utcnow())


def live_account_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT id FROM accounts WHERE mode = 'live' ORDER BY id LIMIT 1").fetchone()["id"]
