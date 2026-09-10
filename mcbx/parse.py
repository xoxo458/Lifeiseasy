"""Stitch raw statement lines into whole transactions.

Both extraction engines emit the same shape - a flat list of ``RawLine``, one per
*visual* line of the printed table. MCB wraps a single transaction over several
visual lines (and across page breaks), so the real work is deciding where one
transaction ends and the next begins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Transaction, parse_amount, parse_date

# Cells whose values are numeric/atomic - a continuation line never adds to these.
_ATOMIC = ("debit", "credit", "balance")
# Cells that carry wrapped text and therefore accumulate across continuation lines.
_TEXT = ("description", "remitter_name", "remitter_iban", "remitter_bank", "ref_no")

_FIELDS = ("tran_date", "effect_date", "branch") + _TEXT + _ATOMIC

# Footer / page-furniture lines that must never become transactions.
_NOISE_RE = re.compile(
    r"^(page\s*[:\d]|user\s*id|note\s*:|total\s+(dr|cr)\s+transactions|"
    r"sum\s+of\s+(dr|cr)\s+transactions|available\s+balance|closing\s+ledger|"
    r"opening\s+balance|account\s+statement|camscanner|for\s+internal\s+use|"
    r"tran\.?\s*date|transaction\s+details|mcb\s+bank)",
    re.IGNORECASE,
)


@dataclass
class RawLine:
    """One visual line of the table, cells already split by column."""

    cells: dict[str, str] = field(default_factory=dict)
    page: int = 0

    def get(self, name: str) -> str:
        return (self.cells.get(name) or "").strip()

    def is_blank(self) -> bool:
        return not any(self.get(f) for f in _FIELDS)


def is_noise(line: RawLine) -> bool:
    """True for footers, column headers and scanner watermarks."""
    joined = " ".join(line.get(f) for f in _FIELDS).strip()
    if not joined:
        return True
    if _NOISE_RE.match(joined):
        return True
    # A footer such as "Total DR Transactions 32" can land in the description cell.
    return bool(_NOISE_RE.match(line.get("description")))


def _starts_transaction(line: RawLine) -> bool:
    """A new transaction begins on the line that carries a transaction date."""
    return parse_date(line.get("tran_date")) is not None


def stitch(lines: list[RawLine], wrap_join: str = "none") -> list[Transaction]:
    """Fold wrapped continuation lines into the transaction they belong to.

    ``wrap_join`` controls how a wrapped cell is rejoined: ``"none"`` concatenates
    directly (MCB breaks mid-word, and it is what the target format shows), while
    ``"space"`` inserts a single space.
    """
    sep = " " if wrap_join == "space" else ""
    out: list[Transaction] = []

    for line in lines:
        if line.is_blank() or is_noise(line):
            continue

        if _starts_transaction(line) or not out:
            if not _starts_transaction(line):
                # Leading continuation with no transaction open (e.g. a page that
                # opens mid-row and whose parent was never captured) - skip it
                # rather than inventing a dateless transaction.
                continue
            out.append(_new_transaction(line))
            continue

        _append_continuation(out[-1], line, sep)

    return [t for t in out if not t.is_empty()]


def _new_transaction(line: RawLine) -> Transaction:
    return Transaction(
        tran_date=parse_date(line.get("tran_date")),
        effect_date=parse_date(line.get("effect_date")) or parse_date(line.get("tran_date")),
        branch=_clean_branch(line.get("branch")),
        description=line.get("description"),
        remitter_name=line.get("remitter_name"),
        remitter_iban=line.get("remitter_iban"),
        remitter_bank=line.get("remitter_bank"),
        ref_no=line.get("ref_no"),
        debit=parse_amount(line.get("debit")),
        credit=parse_amount(line.get("credit")),
        balance=parse_amount(line.get("balance")),
        page=line.page,
    )


def _append_continuation(txn: Transaction, line: RawLine, sep: str) -> None:
    for name in _TEXT:
        extra = line.get(name)
        if not extra:
            continue
        current = getattr(txn, name)
        setattr(txn, name, f"{current}{sep}{extra}" if current else extra)

    # Amounts occasionally land one visual line below their row on a bad scan.
    for name in _ATOMIC:
        if getattr(txn, name) is None:
            value = parse_amount(line.get(name))
            if value is not None:
                setattr(txn, name, value)

    if not txn.branch:
        txn.branch = _clean_branch(line.get("branch"))


def _clean_branch(raw: str) -> str:
    """'0069' -> '69'; leave anything non-numeric untouched."""
    text = raw.strip()
    if text.isdigit():
        return str(int(text))
    return text


def dedupe(transactions: list[Transaction]) -> list[Transaction]:
    """Drop exact repeats (overlapping page chunks can re-report a row)."""
    seen: set[tuple] = set()
    out: list[Transaction] = []
    for txn in transactions:
        key = txn.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(txn)
    return out
