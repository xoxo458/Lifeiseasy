"""Data structures shared by every extraction engine."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

# MCB prints dates as 01-AUG-26 / 17-APR-20.
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_DATE_RE = re.compile(r"^\s*(\d{1,2})[-/\s]([A-Za-z]{3})[-/\s](\d{2}|\d{4})\s*$")


def parse_date(raw: Optional[str]) -> Optional[date]:
    """'01-AUG-26' -> date(2026, 8, 1). Returns None for anything unparseable."""
    if not raw:
        return None
    m = _DATE_RE.match(raw)
    if not m:
        return None
    day, mon, year = m.group(1), m.group(2).upper(), m.group(3)
    if mon not in _MONTHS:
        return None
    y = int(year)
    if len(year) == 2:
        # Statements are contemporary: 00-79 -> 2000s, 80-99 -> 1900s.
        y += 2000 if y < 80 else 1900
    try:
        return date(y, _MONTHS[mon], int(day))
    except ValueError:
        return None


def parse_amount(raw: Optional[str]) -> Optional[Decimal]:
    """'1,144,333.79' -> Decimal. Returns None when the cell is blank."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace(" ", "")
    if not text or text in {"-", "--", "."}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.lstrip("+")
    if text.startswith("-"):
        negative, text = True, text[1:]
    if not re.fullmatch(r"\d*\.?\d+", text):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


@dataclass
class Transaction:
    """One statement line, after wrapped rows have been stitched together."""

    tran_date: Optional[date] = None
    effect_date: Optional[date] = None
    branch: str = ""
    description: str = ""
    remitter_name: str = ""
    remitter_iban: str = ""
    remitter_bank: str = ""
    ref_no: str = ""
    debit: Optional[Decimal] = None
    credit: Optional[Decimal] = None
    balance: Optional[Decimal] = None
    page: Optional[int] = None

    @property
    def signed_amount(self) -> Decimal:
        """Credit positive, debit negative - the effect on the balance."""
        return (self.credit or Decimal(0)) - (self.debit or Decimal(0))

    def is_empty(self) -> bool:
        return not any(
            [self.tran_date, self.description.strip(), self.debit, self.credit, self.balance]
        )

    def dedupe_key(self) -> tuple:
        return (
            self.tran_date,
            self.description,
            str(self.debit),
            str(self.credit),
            str(self.balance),
        )


@dataclass
class StatementMeta:
    """Header block above the transaction table."""

    account_title: str = ""
    address: str = ""
    account_no: str = ""
    iban: str = ""
    account_type: str = ""
    currency: str = ""
    branch: str = ""
    opened_on: Optional[date] = None
    period_from: Optional[date] = None
    period_to: Optional[date] = None
    statement_datetime: str = ""
    opening_balance: Optional[Decimal] = None


@dataclass
class StatementTotals:
    """Footer block printed after the last transaction."""

    total_dr_count: Optional[int] = None
    total_cr_count: Optional[int] = None
    sum_dr: Optional[Decimal] = None
    sum_cr: Optional[Decimal] = None
    available_balance: Optional[Decimal] = None
    closing_balance: Optional[Decimal] = None


@dataclass
class Statement:
    meta: StatementMeta = field(default_factory=StatementMeta)
    totals: StatementTotals = field(default_factory=StatementTotals)
    transactions: list[Transaction] = field(default_factory=list)
    source_file: str = ""
    engine: str = ""

    def to_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "engine": self.engine,
            "meta": _jsonable(asdict(self.meta)),
            "totals": _jsonable(asdict(self.totals)),
            "transactions": [_jsonable(asdict(t)) for t in self.transactions],
        }


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    return obj
