"""
Browser dashboard for the retail inventory tracker.

  python main.py serve
  uvicorn web:app --reload --port 8000
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db import enrich_product, get_all_products, get_connection, get_inventory_stats, init_db
from watcher import (
    INVOICES_DIR,
    get_activity,
    ingest_pdf,
    record_activity,
    start_watcher,
    stop_watcher,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEMO_DIR = ROOT / "demo"

# Public demo: shows results already extracted from demo/sample-invoice.pdf, and turns off
# uploads and the watcher so the site never needs (or spends) a Gemini API key.
DEMO_MODE = os.getenv("DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
DEMO_MESSAGE = "Uploads are turned off in this public demo."

_lock = threading.Lock()
_observer = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _observer
    init_db()
    INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    if DEMO_MODE:
        _load_demo_data()
    yield
    with _lock:
        if _observer is not None and _observer.is_alive():
            stop_watcher(_observer)
            _observer = None


app = FastAPI(title="Retail Inventory Tracker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _load_demo_data() -> None:
    """Replace the inventory with the saved extraction results for the sample invoice."""
    sample = json.loads((DEMO_DIR / "sample-results.json").read_text(encoding="utf-8"))
    products = sample["products"]
    with get_connection() as conn:
        conn.execute("DELETE FROM inventory")
        conn.executemany(
            "INSERT INTO inventory (product_name, expiry_date, last_updated) VALUES (?, ?, ?)",
            [(p["product_name"], p["expiry_date"], p["last_updated"]) for p in products],
        )
    record_activity(
        "ingest",
        f"Stored {len(products)}/{len(products)} products from {sample['source']}",
    )


@app.get("/api/config")
def config() -> dict[str, bool]:
    return {"demo": DEMO_MODE}


@app.get("/sample-invoice.pdf")
def sample_invoice() -> FileResponse:
    return FileResponse(DEMO_DIR / "sample-invoice.pdf", media_type="application/pdf")


def _watcher_running() -> bool:
    obs = _observer
    return obs is not None and obs.is_alive()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/stats")
def stats(days: int = Query(15, ge=0)) -> dict:
    data = get_inventory_stats(days=days)
    data["days"] = days
    return data


@app.get("/api/inventory")
def inventory(
    days: int = Query(15, ge=0),
    status: str = Query("all"),
    q: str = Query(""),
) -> dict:
    allowed = {"all", "expired", "today", "expiring", "healthy", "soon"}
    if status not in allowed:
        raise HTTPException(400, f"status must be one of {sorted(allowed)}")

    needle = q.strip().lower()
    rows = []
    for raw in get_all_products():
        item = enrich_product(raw, days=days)
        if status == "soon" and item["status"] not in {"today", "expiring"}:
            continue
        if status not in {"all", "soon"} and item["status"] != status:
            continue
        if needle and needle not in item["product_name"].lower():
            continue
        rows.append(item)

    return {"days": days, "status": status, "count": len(rows), "products": rows}


@app.get("/api/watcher")
def watcher_status() -> dict[str, object]:
    return {
        "running": _watcher_running(),
        "invoices_dir": str(INVOICES_DIR),
        "activity": get_activity()[:20],
    }


@app.post("/api/watcher/start")
def watcher_start() -> dict[str, object]:
    global _observer
    if DEMO_MODE:
        raise HTTPException(403, DEMO_MESSAGE)
    with _lock:
        if _watcher_running():
            return {"running": True, "message": "Watcher is already running."}
        try:
            _observer = start_watcher(blocking=False)
        except Exception as exc:
            logger.exception("Could not start watcher")
            raise HTTPException(500, f"Failed to start watcher: {exc}") from exc
    return {
        "running": True,
        "message": f"Watching {INVOICES_DIR} — existing PDFs are being processed.",
        "invoices_dir": str(INVOICES_DIR),
        "activity": get_activity()[:20],
    }


@app.post("/api/watcher/stop")
def watcher_stop() -> dict[str, object]:
    global _observer
    with _lock:
        if not _watcher_running():
            return {"running": False, "message": "Watcher is not running."}
        stop_watcher(_observer)
        _observer = None
    return {"running": False, "message": "Watcher stopped.", "activity": get_activity()[:20]}


@app.post("/api/upload")
async def upload_invoice(file: UploadFile = File(...)) -> dict:
    if DEMO_MODE:
        raise HTTPException(403, DEMO_MESSAGE)
    name = Path(file.filename or "invoice.pdf").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF invoices are supported.")

    INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(INVOICES_DIR / name)
    contents = await file.read()
    if not contents:
        raise HTTPException(400, "Uploaded file is empty.")
    dest.write_bytes(contents)

    if _watcher_running():
        return {
            "ok": True,
            "queued": True,
            "saved_as": dest.name,
            "stored": 0,
            "extracted": 0,
            "file": dest.name,
            "message": "Saved. The folder watcher will ingest this PDF.",
        }

    try:
        summary = ingest_pdf(dest)
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc

    return {
        "ok": True,
        "queued": False,
        "saved_as": dest.name,
        **summary,
    }


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1
