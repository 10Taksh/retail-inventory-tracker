"""
Watch ./invoices for new PDFs and upsert extracted products into SQLite.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from db import init_db, upsert_product
from extractor import extract_products_from_pdf

logger = logging.getLogger(__name__)

INVOICES_DIR = Path(__file__).resolve().parent / "invoices"

# Drop-in copies are often still being written when the create event fires.
WRITE_SETTLE_SECONDS = 1.5


class InvoiceHandler(FileSystemEventHandler):
    """Process newly created or moved PDF invoices once."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._seen: set[str] = set()

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
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

        threading.Thread(target=self._process, args=(path,), daemon=True).start()

    def _process(self, path: Path) -> None:
        _wait_for_stable_file(path)
        try:
            ingest_pdf(path)
        except Exception:
            return


def ingest_pdf(path: Path) -> dict[str, int | str]:
    """Extract products from a PDF and upsert them. Returns a small summary."""
    try:
        products = extract_products_from_pdf(path)
    except Exception as exc:
        logger.exception("Failed to extract products from %s: %s", path.name, exc)
        raise

    if not products:
        logger.warning("No products extracted from %s", path.name)
        return {"stored": 0, "extracted": 0, "file": path.name}

    stored = 0
    for product in products:
        try:
            upsert_product(product["product_name"], product["expiry_date"])
            stored += 1
        except Exception as exc:
            logger.warning(
                "Skipping %r from %s: %s",
                product.get("product_name"),
                path.name,
                exc,
            )

    logger.info("Stored %s/%s products from %s", stored, len(products), path.name)
    return {"stored": stored, "extracted": len(products), "file": path.name}


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


def start_watcher(
    directory: Optional[Path] = None,
    blocking: bool = False,
) -> Observer:
    """
    Start monitoring ``directory`` (default: ./invoices).

    Returns the Observer so the CLI can stop it later. When ``blocking`` is
    True, this call runs until KeyboardInterrupt.
    """
    watch_dir = directory or INVOICES_DIR
    watch_dir.mkdir(parents=True, exist_ok=True)
    init_db()

    observer = Observer()
    observer.schedule(InvoiceHandler(), str(watch_dir), recursive=False)
    observer.start()
    logger.info("Watching %s for new PDFs", watch_dir)

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
    logger.info("Invoice watcher stopped")
