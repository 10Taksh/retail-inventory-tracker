"""
PDF text extraction plus Gemini structured JSON parsing.

The LLM only extracts fields. Persistence and date-window queries live in db.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """Extract all products and their expiry dates from this invoice text.
Return ONLY a JSON array of objects with keys 'product_name' (string) and 'expiry_date' (YYYY-MM-DD).
If a date is incomplete, infer the most likely ISO date. Skip rows with no product name.
Do not wrap the array in markdown. Do not add commentary.

Invoice text:
"""

# Schema for Gemini structured outputs (JSON mode).
PRODUCT_ARRAY_SCHEMA: dict[str, Any] = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "product_name": {"type": "STRING"},
            "expiry_date": {"type": "STRING"},
        },
        "required": ["product_name", "expiry_date"],
    },
}

JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")


def extract_pdf_text(pdf_path: str | Path) -> str:
    """Read text from a PDF, preferring pdfplumber and falling back to PyPDF2."""
    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")

    text = _read_with_pdfplumber(path)
    if text.strip():
        return text

    logger.warning("pdfplumber returned no text for %s; trying PyPDF2", path)
    text = _read_with_pypdf2(path)
    if not text.strip():
        raise ValueError(f"No extractable text in PDF: {path}")
    return text


def _read_with_pdfplumber(path: Path) -> str:
    try:
        import pdfplumber
    except ImportError as exc:
        logger.debug("pdfplumber unavailable: %s", exc)
        return ""

    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return "\n".join(pages)


def _read_with_pypdf2(path: Path) -> str:
    from PyPDF2 import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def extract_products_from_text(invoice_text: str, api_key: Optional[str] = None) -> list[dict[str, str]]:
    """
    Send invoice text to Gemini and return a list of {product_name, expiry_date}.

    Uses structured JSON output when the SDK supports it, then runs local
    fallback parsing if the model still returns malformed text.
    """
    cleaned = (invoice_text or "").strip()
    if not cleaned:
        return []

    key = api_key or os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key."
        )

    import google.generativeai as genai

    genai.configure(api_key=key)
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    prompt = EXTRACTION_PROMPT + cleaned

    raw_text = _generate_json(genai, model_name, prompt)
    products = parse_products_json(raw_text)
    return _sanitize_products(products)


def extract_products_from_pdf(pdf_path: str | Path) -> list[dict[str, str]]:
    """Convenience: PDF path -> structured product records."""
    text = extract_pdf_text(pdf_path)
    return extract_products_from_text(text)


def _generate_json(genai: Any, model_name: str, prompt: str) -> str:
    """Call Gemini, first with a response schema, then with JSON MIME only."""
    generation_config: dict[str, Any] = {
        "temperature": 0,
        "response_mime_type": "application/json",
        "response_schema": PRODUCT_ARRAY_SCHEMA,
    }

    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            generation_config=generation_config,
        )
        response = model.generate_content(prompt)
        return _response_text(response)
    except Exception as exc:
        logger.warning("Structured output call failed (%s); retrying JSON MIME only", exc)

    try:
        model = genai.GenerativeModel(
            model_name=model_name,
            generation_config={
                "temperature": 0,
                "response_mime_type": "application/json",
            },
        )
        response = model.generate_content(prompt)
        return _response_text(response)
    except Exception as exc:
        logger.error("Gemini request failed: %s", exc)
        raise RuntimeError(f"Gemini extraction failed: {exc}") from exc


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return text
    # Some SDK versions expose candidates instead of .text
    try:
        return response.candidates[0].content.parts[0].text
    except Exception as exc:
        raise ValueError("Gemini returned an empty response") from exc


def parse_products_json(raw: str) -> list[dict[str, Any]]:
    """
    Parse a JSON array from model output.

    Fallback order:
      1. Direct json.loads
      2. Fenced ```json blocks
      3. First square-bracket array in the text
    """
    if not raw or not str(raw).strip():
        logger.error("Empty LLM response; skipping this file")
        return []

    candidates = [raw.strip()]

    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    if fence:
        candidates.append(fence.group(1).strip())

    match = JSON_ARRAY_RE.search(raw)
    if match:
        candidates.append(match.group(0))

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            logger.debug("JSON parse failed: %s", exc)
            continue

        if isinstance(parsed, dict) and "products" in parsed:
            parsed = parsed["products"]
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list):
            return parsed

    logger.error("Malformed LLM JSON; could not recover a product array")
    return []


def _sanitize_products(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Drop incomplete rows so the DB layer never sees junk names/dates."""
    cleaned: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or item.get("name") or "").strip()
        expiry = str(item.get("expiry_date") or item.get("expiry") or "").strip()
        if not name or not expiry:
            logger.warning("Skipping incomplete product record: %s", item)
            continue
        cleaned.append({"product_name": name, "expiry_date": expiry})
    return cleaned
