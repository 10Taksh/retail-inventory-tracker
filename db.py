"""
SQLite persistence for extracted inventory.

Stores one row per product name. New invoices upsert the expiry date so a
fresh shipment replaces the previous rotation date without creating duplicates.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Optional, Union

# Keep the DB next to the project so queries stay local and portable.
DB_PATH = Path(__file__).resolve().parent / "inventory.db"

DateLike = Union[str, date, datetime]


def _normalize_date(expiry_date: DateLike) -> str:
    """Coerce incoming dates to ISO YYYY-MM-DD for SQLite DATE comparisons."""
    if isinstance(expiry_date, datetime):
        return expiry_date.date().isoformat()
    if isinstance(expiry_date, date):
        return expiry_date.isoformat()

    text = str(expiry_date).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized expiry date format: {expiry_date!r}")


@contextmanager
def get_connection(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Open a connection with row access by column name."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[Path] = None) -> None:
    """Create the inventory table if it does not already exist."""
    with get_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory (
                product_name TEXT PRIMARY KEY,
                expiry_date  TEXT NOT NULL,
                last_updated TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_expiry
            ON inventory (expiry_date)
            """
        )


def upsert_product(
    name: str,
    expiry_date: DateLike,
    db_path: Optional[Path] = None,
) -> None:
    """
    Insert a product or replace its expiry date if the name already exists.

    New shipments are treated as rotation: the latest extracted expiry wins.
    """
    product_name = (name or "").strip()
    if not product_name:
        raise ValueError("product name cannot be empty")

    iso_date = _normalize_date(expiry_date)
    init_db(db_path)

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO inventory (product_name, expiry_date, last_updated)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(product_name) DO UPDATE SET
                expiry_date  = excluded.expiry_date,
                last_updated = CURRENT_TIMESTAMP
            """,
            (product_name, iso_date),
        )


def get_expiring_products(
    days: int = 15,
    db_path: Optional[Path] = None,
    include_expired: bool = True,
) -> list[dict[str, Any]]:
    """
    Return products whose expiry date falls within the next ``days`` days.

    Date math is done in SQL (not in the LLM) so the window is exact.
    Already-expired stock is included by default so rotation does not miss it.
    """
    if days < 0:
        raise ValueError("days must be >= 0")

    init_db(db_path)
    lower_bound = "date('now')" if not include_expired else "'0001-01-01'"

    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT product_name, expiry_date, last_updated
            FROM inventory
            WHERE date(expiry_date) <= date('now', ?)
              AND date(expiry_date) >= {lower_bound}
            ORDER BY date(expiry_date) ASC, product_name ASC
            """,
            (f"+{int(days)} days",),
        ).fetchall()

    return [dict(row) for row in rows]


def get_all_products(db_path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Return every inventory row, soonest expiry first."""
    init_db(db_path)
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT product_name, expiry_date, last_updated
            FROM inventory
            ORDER BY date(expiry_date) ASC, product_name ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_inventory_stats(days: int = 15, db_path: Optional[Path] = None) -> dict[str, int]:
    """Counts for dashboard cards. Date math stays in SQL."""
    if days < 0:
        raise ValueError("days must be >= 0")

    init_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN date(expiry_date) < date('now') THEN 1 ELSE 0 END) AS expired,
                SUM(
                    CASE
                        WHEN date(expiry_date) >= date('now')
                         AND date(expiry_date) <= date('now', ?)
                        THEN 1 ELSE 0
                    END
                ) AS expiring,
                SUM(CASE WHEN date(expiry_date) > date('now', ?) THEN 1 ELSE 0 END) AS healthy
            FROM inventory
            """,
            (f"+{int(days)} days", f"+{int(days)} days"),
        ).fetchone()

    return {
        "total": int(row["total"] or 0),
        "expired": int(row["expired"] or 0),
        "expiring": int(row["expiring"] or 0),
        "healthy": int(row["healthy"] or 0),
    }


def enrich_product(
    row: dict[str, Any],
    days: int = 15,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Add remaining-days and a status label for the UI/CLI."""
    as_of = today or date.today()
    expiry = date.fromisoformat(str(row["expiry_date"]))
    remaining = (expiry - as_of).days
    if remaining < 0:
        status = "expired"
        label = f"Expired {abs(remaining)}d ago"
    elif remaining == 0:
        status = "today"
        label = "Expires today"
    else:
        status = "expiring" if remaining <= days else "healthy"
        label = f"{remaining}d left"

    return {
        **row,
        "days_remaining": remaining,
        "status": status,
        "status_label": label,
    }
