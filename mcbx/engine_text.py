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
    rows: list[list[str]] = []   # every visual line as tokens, for the label scan

    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            page_texts.append(page.extract_text() or "")
            if not words:
                continue
            rows.extend([w["text"] for w in row] for row in _row_groups(words))
            bounds = _column_bounds(words)
            if not bounds:
                continue
            lines.extend(_page_lines(words, bounds, page_no))

    if not lines:
        raise TextLayerError(f"no table rows found in the text layer of {pdf_path}")

    full_text = "\n".join(page_texts)
    statement.meta = parse_meta(full_text, rows)
    statement.totals = parse_totals(rows)
    return lines, statement


def _row_groups(words: list[dict], tolerance: float = _LINE_TOLERANCE) -> list[list[dict]]:
    """Cluster words into visual lines by their vertical position."""
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if rows and abs(word["top"] - rows[-1][0]["top"]) <= tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    return rows


def _column_bounds(words: list[dict], tolerance: float = _LINE_TOLERANCE) -> list[tuple[str, float, float]]:
    """Locate each column's x-range from the printed header row.

    Returns ``[(field, x_start, x_end), ...]``; boundaries sit halfway between
    neighbouring header labels, which keeps right-aligned amount columns intact.
    """
    for row in _row_groups(words, tolerance):
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
    words: list[dict], bounds: list[tuple[str, float, float]], page_no: int,
    tolerance: float = _LINE_TOLERANCE,
) -> list[RawLine]:
    """Assign every word on the page to a column, one RawLine per visual line."""
    header_bottom = _header_bottom(words, tolerance)
    lines: list[RawLine] = []

    for row in _row_groups(words, tolerance):
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


def _header_bottom(words: list[dict], tolerance: float = _LINE_TOLERANCE) -> float:
    for row in _row_groups(words, tolerance):
        text = " ".join(w["text"] for w in row)
        if "Balance" in text and "Debit" in text and "Date" in text:
            return max(w["bottom"] for w in row)
    return 0.0


# --- labelled values in the header / footer blocks ---------------------------

# MCB prints two label/value pairs side by side:
#
#   Total DR Transactions    125        Available Balance:   3,563,090.00
#
# Flattened to text, both numbers follow the first label, so a regex grabs
# whichever comes first. Match the label inside a visual line instead and take
# the first number to its right, stopping at the next label.
_LABELLED = [
    ("Total DR Transactions", "total_dr_count", "count"),
    ("Total CR Transactions", "total_cr_count", "count"),
    ("Sum of DR Transactions", "sum_dr", "amount"),
    ("Sum of CR Transactions", "sum_cr", "amount"),
    ("Available Balance", "available_balance", "amount"),
    ("Closing Ledger Balance", "closing_balance", "amount"),
    ("Opening Balance", "opening_balance", "amount"),
]

# A count is a bare integer; an amount carries MCB's two decimal places. That
# distinction stops a balance being read as a transaction count.
_COUNT_RE = re.compile(r"^\d[\d,]*$")
_AMOUNT_RE = re.compile(r"^\d[\d,]*\.\d{2}$")


def _norm_token(token: str) -> str:
    return token.rstrip(".:").lower()


def _label_at(tokens: list[str], index: int, label_tokens: list[str]) -> bool:
    if index + len(label_tokens) > len(tokens):
        return False
    return all(
        _norm_token(tokens[index + offset]) == label_tokens[offset]
        for offset in range(len(label_tokens))
    )


_LABEL_STARTS = [[_norm_token(t) for t in label.split()] for label, _, _ in _LABELLED]


def _value_after(tokens: list[str], start: int, kind: str) -> str | None:
    pattern = _COUNT_RE if kind == "count" else _AMOUNT_RE
    for index in range(start, min(len(tokens), start + 5)):
        if any(_label_at(tokens, index, label) for label in _LABEL_STARTS):
            return None  # the next pair begins; this one's value column is blank
        if pattern.match(tokens[index]):
            return tokens[index]
    return None


def scan_labelled_values(rows: list[list[str]]) -> dict[str, str]:
    """Find each summary label in the printed lines and read the value beside it."""
    found: dict[str, str] = {}
    for tokens in rows:
        for label, field, kind in _LABELLED:
            if field in found:
                continue
            label_tokens = [_norm_token(t) for t in label.split()]
            for index in range(len(tokens)):
                if not _label_at(tokens, index, label_tokens):
                    continue
                value = _value_after(tokens, index + len(label_tokens), kind)
                if value is not None:
                    found[field] = value
                break
    return found


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


def parse_meta(text: str, rows: list[list[str]] | None = None) -> StatementMeta:
    """Pull the account block above the table out of the raw page text."""
    meta = StatementMeta()
    scanned = scan_labelled_values(rows or [])
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
    meta.opening_balance = parse_amount(scanned.get("opening_balance"))

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    title = next(
        (ln for ln in lines[:6] if "LIMITED" in ln.upper() and "MCB BANK" not in ln.upper()),
        "",
    )
    meta.account_title = _trim_right_column(title)
    return meta


def parse_totals(rows: list[list[str]]) -> StatementTotals:
    """Pull the summary block printed after the last transaction."""
    scanned = scan_labelled_values(rows)

    def count(field: str) -> int | None:
        value = (scanned.get(field) or "").replace(",", "")
        return int(value) if value.isdigit() else None

    return StatementTotals(
        total_dr_count=count("total_dr_count"),
        total_cr_count=count("total_cr_count"),
        sum_dr=parse_amount(scanned.get("sum_dr")),
        sum_cr=parse_amount(scanned.get("sum_cr")),
        available_balance=parse_amount(scanned.get("available_balance")),
        closing_balance=parse_amount(scanned.get("closing_balance")),
    )
