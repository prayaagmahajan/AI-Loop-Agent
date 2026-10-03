"""SQLite sandbox and execution-result grading (no model required)."""
import collections
from contextlib import closing
import math
import sqlite3
import time
from pathlib import Path

MAX_ROWS = 10000


def execute(db, sql):
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError('Missing SQL')
    conn = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True)
    deadline = time.monotonic() + 2
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    def authorize(action, a, b, *_):
        if action == sqlite3.SQLITE_READ and a not in {'Album','Artist','Customer','Employee','Genre','Invoice','InvoiceLine','MediaType','Playlist','PlaylistTrack','Track'}:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (b or '').lower() in {'load_extension','readfile','writefile'}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
    conn.set_authorizer(authorize)
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(MAX_ROWS + 1)
        if len(rows) > MAX_ROWS:
            raise ValueError('Result exceeds 10000-row safety limit')
        return rows
    finally:
        conn.close()


def normalized(rows):
    # Currency and floating aggregation differ by rounding order; preserve NULL,
    # text, column position, and duplicate multiplicity. 6 decimal digits suffice here.
    def value(v):
        if isinstance(v, (int, float)):
            if not math.isfinite(v):
                raise ValueError('Non-finite result')
            return ('number', round(v, 6))
        return (type(v).__name__, v)
    return [tuple(value(v) for v in row) for row in rows]


def equal_rows(actual, expected, ordered=False):
    a, b = normalized(actual), normalized(expected)
    return a == b if ordered else collections.Counter(a) == collections.Counter(b)


def schema(db):
    with closing(sqlite3.connect(db)) as conn:
        return '\n'.join(row[0] for row in conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))


def mutation_fixture(source, target, version='mutations-v1'):
    """Deterministic counterexample database: ties, quantities, historical prices."""
    if version not in ('mutations-v1', 'mutations-v2'):
        raise ValueError('Unknown fixture version')
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst, dst:
        src.backup(dst)
        dst.execute('UPDATE InvoiceLine SET Quantity=3, UnitPrice=7.25 WHERE InvoiceLineId=1')
        dst.execute('UPDATE Invoice SET Total=100000 WHERE InvoiceId IN (1,2)')
        # Force an exact top customer tie independently of previous invoice totals.
        ids = [r[0] for r in dst.execute('SELECT CustomerId FROM Invoice WHERE InvoiceId IN (1,2)')]
        for cid in ids:
            dst.execute('UPDATE Invoice SET Total=0 WHERE CustomerId=? AND InvoiceId NOT IN (1,2)', (cid,))
        dst.execute("INSERT INTO Artist(ArtistId,Name) VALUES (99999,'Eval artist without albums')")
        if version == 'mutations-v2':
            dst.execute("INSERT INTO Invoice(InvoiceId,CustomerId,InvoiceDate,BillingCountry,Total) VALUES (99999,59,'2010-12-31 12:00:00','USA',1.0)")