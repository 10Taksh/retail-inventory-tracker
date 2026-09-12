"""
Watch ./invoices for new PDFs and upsert extracted products into SQLite.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from watchdog.events import (
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from db import init_db, upsert_product
from extractor import extract_products_from_pdf

logger = logging.getLogger(__name__)

INVOICES_DIR = Path(__file__).resolve().parent / "invoices"

# Drop-in copies are often still being written when the create event fires.
WRITE_SETTLE_SECONDS = 1.5

_activity: deque[dict[str, Any]] = deque(maxlen=80)
_activity_lock = threading.Lock()


def record_activity(kind: str, message: str, extra: Optional[dict[str, Any]] = None) -> None:
    item: dict[str, Any] = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "message": message,
    }
    if extra:
        item.update(extra)
    with _activity_lock:
        _activity.appendleft(item)
    logger.info("%s", message)
    print(message, flush=True)


def get_activity() -> list[dict[str, Any]]:
    with _activity_lock:
        return list(_activity)


class InvoiceHandler(FileSystemEventHandler):
    """Process newly created, modified, or moved PDF invoices once."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._seen: set[str] = set()

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._handle(Path(str(event.src_path)))

    def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._handle(Path(str(event.src_path)))

    def on_moved(self, event: FileMovedEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._handle(Path(str(event.dest_path)))

    def _handle(self, path: Path) -> None:
        if path.suffix.lower() != ".pdf":
            return
        if path.name.startswith((".", "~", "$")):
            return

        key = str(path.resolve()) if path.exists() else str(path)
        with self._lock:
            if key in self._seen:
                return
            self._seen.add(key)

        record_activity("queued", f"Queued {path.name}")
        threading.Thread(target=self._process, args=(path,), daemon=True).start()

    def _process(self, path: Path) -> None:
        _wait_for_stable_file(path)
        try:
            ingest_pdf(path)
        except Exception as exc:
            record_activity("error", f"Failed {path.name}: {exc}")


def ingest_pdf(path: Path) -> dict[str, Any]:
    """Extract products from a PDF and upsert them. Returns a small summary."""
    try:
        products = extract_products_from_pdf(path)
    except Exception as exc:
        logger.exception("Failed to extract products from %s: %s", path.name, exc)
        raise

    if not products:
        record_activity("empty", f"No products extracted from {path.name}")
        return {"stored": 0, "extracted": 0, "file": path.name, "products": []}

    stored = 0
    kept: list[dict[str, str]] = []
    for product in products:
        try:
            upsert_product(product["product_name"], product["expiry_date"])
            stored += 1
            kept.append(product)
            print(f"  {product['product_name']}  {product['expiry_date']}", flush=True)
        except Exception as exc:
            logger.warning(
                "Skipping %r from %s: %s",
                product.get("product_name"),
                path.name,
                exc,
            )

    record_activity(
        "ingest",
        f"Stored {stored}/{len(products)} products from {path.name}",
        {"file": path.name, "stored": stored, "extracted": len(products), "products": kept},
    )
    return {
        "stored": stored,
        "extracted": len(products),
        "file": path.name,
        "products": kept,
    }


def _wait_for_stable_file(path: Path, timeout: float = 15.0) -> None:
    """Wait until size stops changing so we do not parse a half-written PDF."""
    deadline = time.time() + timeout
    last_size = -1
    while time.time() < deadline:
        if not path.exists():
            time.sleep(0.2)
            continue
        size = path.stat().st_size
        if size > 0 and size == last_size:
            time.sleep(WRITE_SETTLE_SECONDS)
            if path.stat().st_size == size:
                return
        last_size = size
        time.sleep(0.3)
    logger.warning("Timed out waiting for %s to finish writing", path.name)


def scan_existing_pdfs(directory: Optional[Path] = None, handler: Optional[InvoiceHandler] = None) -> int:
    """Queue every PDF already in the invoices folder."""
    watch_dir = directory or INVOICES_DIR
    worker = handler or InvoiceHandler()
    pdfs = sorted(p for p in watch_dir.glob("*.pdf") if p.is_file())
    for pdf in pdfs:
        worker._handle(pdf)
    return len(pdfs)


def start_watcher(
    directory: Optional[Path] = None,
    blocking: bool = False,
) -> Observer:
    """
    Start monitoring ``directory`` (default: ./invoices).

    Also queues PDFs that are already in the folder (watchdog only sees new events).
    """
    watch_dir = directory or INVOICES_DIR
    watch_dir.mkdir(parents=True, exist_ok=True)
    init_db()

    handler = InvoiceHandler()
    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=False)
    observer.start()
    queued = scan_existing_pdfs(watch_dir, handler)
    record_activity(
        "watch",
        f"Watching {watch_dir} — queued {queued} existing PDF(s)",
        {"invoices_dir": str(watch_dir), "queued": queued},
    )

    if blocking:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()

    return observer


def stop_watcher(observer: Observer) -> None:
    """Stop a previously started observer."""
    observer.stop()
    observer.join(timeout=5)
    record_activity("watch", "Invoice watcher stopped")
