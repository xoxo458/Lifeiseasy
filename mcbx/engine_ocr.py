"""Offline extraction engine: local Tesseract OCR, no API and no network.

Pages are rendered to greyscale bitmaps, OCR'd with Tesseract, and the resulting
word boxes are fed through the same column-detection logic the text engine uses -
Tesseract's TSV output has the same shape as pdfplumber's word dicts.

Accuracy is materially lower than the vision engine on scanned statements,
especially for digits. Nothing here is trusted: the balance chain and footer
reconciliation in validate.py are what tell you which rows to check by hand.
"""

from __future__ import annotations

import csv
import io
import shutil
import subprocess
from tempfile import NamedTemporaryFile

import pypdfium2 as pdfium

from .engine_text import _column_bounds, _page_lines, parse_meta, parse_totals
from .models import Statement
from .parse import RawLine

DEFAULT_SCALE = 4.0  # render multiplier; ~300dpi at scale 4 for A4
MIN_CONFIDENCE = 30.0  # Tesseract confidence below this is usually watermark noise


class OcrEngineError(RuntimeError):
    pass


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def extract(
    pdf_path: str,
    scale: float = DEFAULT_SCALE,
    min_confidence: float = MIN_CONFIDENCE,
    progress=None,
) -> tuple[list[RawLine], Statement]:
    if not tesseract_available():
        raise OcrEngineError(
            "tesseract is not installed - install it (apt install tesseract-ocr, "
            "brew install tesseract) or use --engine vision"
        )

    statement = Statement(source_file=pdf_path, engine="ocr")
    lines: list[RawLine] = []
    page_texts: list[str] = []

    doc = pdfium.PdfDocument(pdf_path)
    try:
        total = len(doc)
        for index in range(total):
            image = doc[index].render(scale=scale, grayscale=True).to_pil()
            words, text = _ocr_page(image, min_confidence)
            page_texts.append(text)
            if progress:
                progress(index + 1, total)
            if not words:
                continue
            # Tolerance scales with render size: a text line is ~10pt tall.
            tolerance = 3.0 * scale
            bounds = _column_bounds(words, tolerance)
            if not bounds:
                continue
            lines.extend(_page_lines(words, bounds, index + 1, tolerance))
    finally:
        doc.close()

    if not lines:
        raise OcrEngineError(
            f"OCR found no table rows in {pdf_path} - the scan may be too low "
            "resolution; try a higher --ocr-scale"
        )

    full_text = "\n".join(page_texts)
    statement.meta = parse_meta(full_text)
    statement.totals = parse_totals(full_text)
    return lines, statement


def _ocr_page(image, min_confidence: float) -> tuple[list[dict], str]:
    """Run Tesseract over one page image, returning word boxes and plain text."""
    with NamedTemporaryFile(suffix=".png") as handle:
        image.save(handle.name)
        tsv = _run_tesseract(handle.name, ["--psm", "6", "-c", "preserve_interword_spaces=1", "tsv"])
        text = _run_tesseract(handle.name, ["--psm", "6"])

    words: list[dict] = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        token = (row.get("text") or "").strip()
        if not token:
            continue
        try:
            confidence = float(row["conf"])
            left, top = float(row["left"]), float(row["top"])
            width, height = float(row["width"]), float(row["height"])
        except (KeyError, TypeError, ValueError):
            continue
        # Low-confidence fragments on this layout are almost always watermark
        # strokes read as punctuation; they would land in random columns.
        if confidence < min_confidence and not any(c.isalnum() for c in token):
            continue
        words.append(
            {"text": token, "x0": left, "x1": left + width, "top": top, "bottom": top + height}
        )
    return words, text


def _run_tesseract(image_path: str, args: list[str]) -> str:
    result = subprocess.run(
        ["tesseract", image_path, "stdout", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise OcrEngineError(f"tesseract failed: {result.stderr.strip()[:200]}")
    return result.stdout
