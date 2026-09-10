"""Arithmetic checks that prove a conversion is faithful to the statement.

The statement is self-verifying: every row's balance must equal the previous
balance plus credit minus debit, and the printed footer totals must match the
rows we extracted. Anything that fails here is surfaced rather than silently
shipped, because a plausible-looking wrong number is the expensive failure mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import Statement, Transaction

CENT = Decimal("0.01")


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    row: int | None  # 1-based index into the transaction list
    check: str
    detail: str


@dataclass
class Report:
    issues: list[Issue]
    checked_rows: int
    balance_chain_ok: bool
    totals_ok: bool

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


def validate(statement: Statement) -> Report:
    txns = statement.transactions
    issues: list[Issue] = []

    issues += _check_rows(txns)
    chain_issues = _check_balance_chain(txns, statement.meta.opening_balance)
    total_issues = _check_totals(statement)
    issues += chain_issues + total_issues

    return Report(
        issues=issues,
        checked_rows=len(txns),
        balance_chain_ok=not chain_issues,
        totals_ok=not total_issues,
    )


def _check_rows(txns: list[Transaction]) -> list[Issue]:
    issues: list[Issue] = []
    for i, txn in enumerate(txns, start=1):
        if txn.tran_date is None:
            issues.append(Issue("error", i, "date", "row has no readable transaction date"))
        if txn.debit is None and txn.credit is None:
            issues.append(Issue("error", i, "amount", "row has neither a debit nor a credit"))
        if txn.debit is not None and txn.credit is not None:
            issues.append(
                Issue("error", i, "amount", f"row has both a debit ({txn.debit}) and a credit ({txn.credit})")
            )
        if txn.balance is None:
            issues.append(Issue("error", i, "balance", "row has no readable balance"))
        if not txn.description.strip():
            issues.append(Issue("warning", i, "description", "row has an empty description"))
    return issues


def _check_balance_chain(txns: list[Transaction], opening: Decimal | None) -> list[Issue]:
    """balance[n] must equal balance[n-1] + credit - debit, to the cent."""
    issues: list[Issue] = []
    previous = opening

    for i, txn in enumerate(txns, start=1):
        if txn.balance is None:
            previous = None
            continue
        if previous is not None:
            expected = (previous + txn.signed_amount).quantize(CENT)
            actual = txn.balance.quantize(CENT)
            if expected != actual:
                issues.append(
                    Issue(
                        "error",
                        i,
                        "balance-chain",
                        f"expected {expected} from {previous} {'+' if txn.signed_amount >= 0 else '-'} "
                        f"{abs(txn.signed_amount)}, statement shows {actual} "
                        f"(off by {actual - expected})",
                    )
                )
        previous = txn.balance
    return issues


def _check_totals(statement: Statement) -> list[Issue]:
    """Reconcile extracted rows against the statement's own footer totals."""
    issues: list[Issue] = []
    totals = statement.totals
    txns = statement.transactions

    dr_rows = [t for t in txns if t.debit is not None]
    cr_rows = [t for t in txns if t.credit is not None]
    sum_dr = sum((t.debit for t in dr_rows), Decimal(0)).quantize(CENT)
    sum_cr = sum((t.credit for t in cr_rows), Decimal(0)).quantize(CENT)

    def compare(label, extracted, printed, check):
        if printed is None:
            issues.append(Issue("warning", None, check, f"statement does not print {label}; not reconciled"))
        elif extracted != printed:
            issues.append(
                Issue("error", None, check, f"{label}: extracted {extracted}, statement prints {printed}")
            )

    compare("debit count", len(dr_rows), totals.total_dr_count, "count-dr")
    compare("credit count", len(cr_rows), totals.total_cr_count, "count-cr")
    compare("sum of debits", sum_dr, totals.sum_dr.quantize(CENT) if totals.sum_dr else None, "sum-dr")
    compare("sum of credits", sum_cr, totals.sum_cr.quantize(CENT) if totals.sum_cr else None, "sum-cr")

    if txns and txns[-1].balance is not None and totals.closing_balance is not None:
        last = txns[-1].balance.quantize(CENT)
        printed = totals.closing_balance.quantize(CENT)
        if last != printed:
            issues.append(
                Issue(
                    "error",
                    None,
                    "closing-balance",
                    f"last row balance {last} != printed closing balance {printed}",
                )
            )
    return issues


def summarise(report: Report) -> str:
    lines = [
        f"rows: {report.checked_rows}",
        f"balance chain: {'OK' if report.balance_chain_ok else 'FAILED'}",
        f"footer totals: {'OK' if report.totals_ok else 'FAILED'}",
    ]
    for issue in report.issues:
        where = f"row {issue.row}" if issue.row else "statement"
        lines.append(f"  [{issue.severity}] {where} / {issue.check}: {issue.detail}")
    return "\n".join(lines)
