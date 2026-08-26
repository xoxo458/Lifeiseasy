"""Extraction engine for PDFs that carry a real text layer.

Columns are located from the printed header row ("Tran. Date  Effect Date ...")
rather than hard-coded offsets, so the engine survives small layout shifts
between MCB statement runs.
"""

from __future__ import annotations

import re

import pdfplumber

from .models import Statement, StatementMeta, StatementTotals, parse_amount, parse_date
from .parse import RawLine

# Header label -> field name, in printed left-to-right order.
_HEADER_LABELS: list[tuple[str, str]] = [
    ("Tran. Date", "tran_date"),
    ("Effect Date", "effect_date"),
    ("Tran. Br.", "branch"),
    ("Transaction Details", "description"),
    ("Remitter Name", "remitter_name"),
    ("Remitter IBAN", "remitter_iban"),
    ("Remitter Bank", "remitter_bank"),
    ("Chq / Ref No", "ref_no"),
    ("Debit", "debit"),
    ("Credit", "credit"),
    ("Balance", "balance"),
]

_LINE_TOLERANCE = 3.0  # points; words within this vertical distance share a line


class TextLayerError(RuntimeError):
    """Raised when the PDF has no usable text layer."""


def has_text_layer(pdf_path: str, min_chars_per_page: int = 200) -> bool:
    """Cheap probe: a scanned PDF yields almost no characters."""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:3]:
            if len(page.chars) >= min_chars_per_page:
                return True
    return False


def extract(pdf_path: str) -> tuple[list[RawLine], Statement]:
    """Return the table's visual lines plus a Statement carrying meta/totals."""
    lines: list[RawLine] = []
    statement = Statement(source_file=pdf_path, engine="text")
    page_texts: list[str] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            page_texts.append(page.extract_text() or "")
            if not words:
                continue
            bounds = _column_bounds(words)
            if not bounds:
                continue
            lines.extend(_page_lines(words, bounds, page_no))

    if not lines:
        raise TextLayerError(f"no table rows found in the text layer of {pdf_path}")

    full_text = "\n".join(page_texts)
    statement.meta = parse_meta(full_text)
    statement.totals = parse_totals(full_text)
    return lines, statement


def _row_groups(words: list[dict]) -> list[list[dict]]:
    """Cluster words into visual lines by their vertical position."""
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if rows and abs(word["top"] - rows[-1][0]["top"]) <= _LINE_TOLERANCE:
            rows[-1].append(word)
        else:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    return rows


def _column_bounds(words: list[dict]) -> list[tuple[str, float, float]]:
    """Locate each column's x-range from the printed header row.

    Returns ``[(field, x_start, x_end), ...]``; boundaries sit halfway between
    neighbouring header labels, which keeps right-aligned amount columns intact.
    """
    for row in _row_groups(words):
        text = " ".join(w["text"] for w in row)
        if "Balance" not in text or "Debit" not in text or "Date" not in text:
            continue

        spans: list[tuple[str, float, float]] = []
        for label, fieldname in _HEADER_LABELS:
            span = _find_label(row, label)
            if span:
                spans.append((fieldname, span[0], span[1]))
        if len(spans) < 6:  # not the header row after all
            continue

        spans.sort(key=lambda s: s[1])
        bounds: list[tuple[str, float, float]] = []
        for i, (fieldname, x0, x1) in enumerate(spans):
            left = 0.0 if i == 0 else (spans[i - 1][2] + x0) / 2
            right = 1e6 if i == len(spans) - 1 else (x1 + spans[i + 1][1]) / 2
            bounds.append((fieldname, left, right))
        return bounds
    return []


def _find_label(row: list[dict], label: str) -> tuple[float, float] | None:
    """Match a possibly multi-word header label against consecutive words."""
    tokens = label.split()
    texts = [w["text"] for w in row]
    for i in range(len(texts) - len(tokens) + 1):
        window = texts[i : i + len(tokens)]
        if all(a.rstrip(".").lower() == b.rstrip(".").lower() for a, b in zip(window, tokens)):
            return row[i]["x0"], row[i + len(tokens) - 1]["x1"]
    return None


def _page_lines(
    words: list[dict], bounds: list[tuple[str, float, float]], page_no: int
) -> list[RawLine]:
    """Assign every word on the page to a column, one RawLine per visual line."""
    header_bottom = _header_bottom(words)
    lines: list[RawLine] = []

    for row in _row_groups(words):
        if row[0]["top"] <= header_bottom:
            continue
        cells: dict[str, list[str]] = {}
        for word in row:
            centre = (word["x0"] + word["x1"]) / 2
            for fieldname, left, right in bounds:
                if left <= centre < right:
                    cells.setdefault(fieldname, []).append(word["text"])
                    break
        if cells:
            lines.append(
                RawLine(cells={k: " ".join(v) for k, v in cells.items()}, page=page_no)
            )
    return lines


def _header_bottom(words: list[dict]) -> float:
    for row in _row_groups(words):
        text = " ".join(w["text"] for w in row)
        if "Balance" in text and "Debit" in text and "Date" in text:
            return max(w["bottom"] for w in row)
    return 0.0


# --- header / footer blocks -------------------------------------------------

def _search(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


# The header's left and right columns land on one extracted line; these labels
# mark where the right-hand account block begins.
_RIGHT_COLUMN_RE = re.compile(
    r"\s*(Account\s*No|IBAN|Account\s*Type|Date of Account Open|Statement\s*(Period|Date))\b",
    re.IGNORECASE,
)


def _trim_right_column(line: str) -> str:
    """Drop the right-hand header block that shares a line with the title."""
    m = _RIGHT_COLUMN_RE.search(line)
    return (line[: m.start()] if m else line).strip()


def parse_meta(text: str) -> StatementMeta:
    """Pull the account block above the table out of the raw page text."""
    meta = StatementMeta()
    meta.account_no = _search(r"Account\s*No\.?\s*:?\s*([0-9]{6,})", text)
    meta.iban = _search(r"IBAN\s*:?\s*([A-Z]{2}[0-9A-Z]{10,32})", text)

    acct_type = _search(r"Account\s*Type\s*/?\s*CCY\s*:?\s*([A-Z]+)\s*/\s*[A-Z]{3}", text)
    meta.account_type = acct_type
    meta.currency = _search(r"Account\s*Type\s*/?\s*CCY\s*:?\s*[A-Z]+\s*/\s*([A-Z]{3})", text)

    meta.opened_on = parse_date(_search(r"Date of Account Open\s*:?\s*([0-9]{1,2}-[A-Za-z]{3}-[0-9]{2,4})", text))
    meta.period_from = parse_date(_search(r"From Date\s*:?\s*([0-9]{1,2}-[A-Za-z]{3}-[0-9]{2,4})", text))
    meta.period_to = parse_date(_search(r"To Date\s*:?\s*([0-9]{1,2}-[A-Za-z]{3}-[0-9]{2,4})", text))
    meta.statement_datetime = _search(r"Statement Date & Time\s*:?\s*(.+)", text)
    meta.branch = _search(r"^\s*(\d{3,4}-[A-Z][A-Z ]+)\s*$", text)
    meta.opening_balance = parse_amount(
        _search(r"Opening Balance.*?Ledger\s*:?\s*([0-9,]+\.\d{2})", text)
        or _search(r"Opening Balance\D*([0-9,]+\.\d{2})", text)
    )

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    title = next(
        (ln for ln in lines[:6] if "LIMITED" in ln.upper() and "MCB BANK" not in ln.upper()),
        "",
    )
    meta.account_title = _trim_right_column(title)
    return meta


def parse_totals(text: str) -> StatementTotals:
    """Pull the summary block printed after the last transaction."""
    totals = StatementTotals()
    dr_count = _search(r"Total DR Transactions\s*:?\s*([0-9,]+)", text)
    cr_count = _search(r"Total CR Transactions\s*:?\s*([0-9,]+)", text)
    totals.total_dr_count = int(dr_count.replace(",", "")) if dr_count else None
    totals.total_cr_count = int(cr_count.replace(",", "")) if cr_count else None
    totals.sum_dr = parse_amount(_search(r"Sum of DR Transactions\s*:?\s*([0-9,]+\.\d{2})", text))
    totals.sum_cr = parse_amount(_search(r"Sum of CR Transactions\s*:?\s*([0-9,]+\.\d{2})", text))
    totals.available_balance = parse_amount(
        _search(r"Available Balance\s*:?\s*([0-9,]+\.\d{2})", text)
    )
    totals.closing_balance = parse_amount(
        _search(r"Closing Ledger Balance\s*:?\s*([0-9,]+\.\d{2})", text)
    )
    return totals
