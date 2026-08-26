"""Generate a digital MCB-layout statement PDF for testing the text engine.

The rows mirror the real sample statement (including wrapped descriptions, a row
that wraps across a page break, and the footer totals block).
"""

from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# (label, x, align) - align "r" for the right-aligned amount columns.
COLS = [
    ("Tran. Date", 30, "l"), ("Effect Date", 88, "l"), ("Tran. Br.", 148, "l"),
    ("Transaction Details", 186, "l"), ("Remitter Name", 300, "l"),
    ("Remitter IBAN", 366, "l"), ("Remitter Bank", 434, "l"),
    ("Chq / Ref No", 496, "l"), ("Debit", 610, "r"), ("Credit", 690, "r"),
    ("Balance", 790, "r"),
]

# Each row: dates, branch, description lines, remitter name lines, iban lines,
# bank, ref, debit, credit, balance.
ROWS = [
    ("01-AUG-26", "01-AUG-26", "1160", ["MONTHLY BUNDLE", "SERVICES CHARGE/"], [], [], "", "", "1.00", "", "10,509,604.87"),
    ("01-AUG-26", "01-AUG-26", "1160", ["FEDERAL EXCISE", "DUTY TAX/"], [], [], "", "", "0.16", "", "10,509,604.71"),
    ("01-AUG-26", "01-AUG-26", "5398", ["P2P RECEIVING VIA", "RAAST-OFFUS/", "Purpose: -"],
     ["JUNAID", "AHMAD BAIG"], ["PK54MEZN001", "252010732301", "2"], "", "", "", "94,000.00", "10,603,604.71"),
    ("01-AUG-26", "01-AUG-26", "1160", ["SETTLEMENT", "RECEIPT/ME-AUTO", "FLEET ~"], [], [], "", "", "28,647.64", "", "10,574,957.07"),
    ("03-AUG-26", "03-AUG-26", "5398", ["P2P RECEIVING VIA", "RAAST-OFFUS/", "Purpose: -"],
     ["ADNAN", "SHAH"], ["PK82AIIN0"], "", "", "", "700,000.00", "11,274,957.07"),
    ("05-AUG-26", "05-AUG-26", "0069", ["CMD TRANSFER", "CREDIT/FUNDSTRF-", "08OCT24MKT-", "2390,;IDPPD  - Imarat",
                                        "Developers Private", "Limited (PayDirect)"], [], [], "", "", "", "10,000,000.00", "21,274,957.07"),
    ("06-AUG-26", "06-AUG-26", "1160", ["FEDERAL EXCISE", "DUTY TAX/AS PER", "CUSTOMER REQUEST"], [], [], "", "9671", "112.00", "", "21,274,845.07"),
    ("17-AUG-26", "17-AUG-26", "1160", ["INWARD CHEQUE", "CLEARING", "DEBIT/2066146083 - TM",
                                        "NIFT CUSTOMER", "CHEQUE:2066146083"], [], [], "", "2066146083", "2,500,000.00", "", "18,774,845.07"),
    ("18-AUG-26", "18-AUG-26", "1771", ["OUTWARD CHEQUE", "CLEARING CREDIT/"], [], [], "", "0000014203", "", "1,500,000.00", "20,274,845.07"),
]

OPENING = "10,509,605.87"
# Derived from ROWS so the fixture is internally consistent.
TOTALS = {
    "dr_count": 5, "cr_count": 4,
    "sum_dr": "2,528,760.80", "sum_cr": "12,294,000.00",
    "closing": "20,274,845.07",
}

LEADING = 11
ROW_GAP = 4

# The "INWARD CHEQUE CLEARING" row is split across the page break: its first
# SPLIT_AFTER description lines print on page 1, the rest on page 2.
SPLIT_ROW = 7
SPLIT_AFTER = 2


def _text(c, x, y, s, align="l", font="Helvetica", size=7.5):
    c.setFont(font, size)
    if align == "r":
        c.drawRightString(x, y, s)
    else:
        c.drawString(x, y, s)


def _page_header(c, width, height, first_page: bool):
    _text(c, 30, height - 30, "MCB Bank Limited", font="Helvetica-Bold", size=11)
    _text(c, 30, height - 44, "AMAZON MARKETING (SMC-PRIVATE) LIMITED", font="Helvetica-Bold", size=8)
    _text(c, 30, height - 56, "AMAZON MARKETING (SMC-PRIVATE) LIMITED 4TH FLOOR BEVERLY CENTRE BLUE AREA ISLAMABAD", size=7)
    _text(c, width - 300, height - 30, "Account Statement", font="Helvetica-Bold", size=11)
    _text(c, width - 300, height - 46, "Account No:  1175809501009469", size=8)
    _text(c, width - 300, height - 58, "IBAN:  PK90MUCB1175809501009469", size=8)
    _text(c, width - 300, height - 70, "Account Type / CCY:  BUS / PKR", size=8)
    _text(c, width - 300, height - 82, "Date of Account Open:  17-APR-20", size=8)
    _text(c, width - 300, height - 94, "Statement Period:  From Date: 01-AUG-26 To Date 19-AUG-26", size=8)
    _text(c, width - 300, height - 106, "Statement Date & Time:  Aug 19, 2026 09:39:13 AM", size=8)
    _text(c, 30, height - 120, "1160-ISLAMABAD JINAH SUPER MKT", font="Helvetica-Bold", size=8)
    if first_page:
        _text(c, width - 330, height - 140, "Opening Balance   Ledger:", font="Helvetica-Bold", size=8)
        _text(c, 830, height - 140, OPENING, align="r", font="Helvetica-Bold", size=8)

    y = height - 158
    for label, x, align in COLS:
        _text(c, x, y, label, align=align, font="Helvetica-Bold", size=7.5)
    c.line(25, y - 4, 840, y - 4)
    return y - 16


def _draw_row(c, y, row, max_desc_lines=None):
    """Draw one transaction; returns the new y. max_desc_lines truncates the
    description so the remainder can be printed on the next page."""
    tdate, edate, branch, desc, name, iban, bank, ref, debit, credit, balance = row
    if max_desc_lines is not None:
        desc = desc[:max_desc_lines]
    depth = max(len(desc), len(name), len(iban), 1)

    for i in range(depth):
        line_y = y - i * LEADING
        if i == 0:
            _text(c, 30, line_y, tdate)
            _text(c, 88, line_y, edate)
            _text(c, 148, line_y, branch)
            if ref:
                _text(c, 496, line_y, ref)
            if debit:
                _text(c, 610, line_y, debit, align="r")
            if credit:
                _text(c, 690, line_y, credit, align="r")
            if balance:
                _text(c, 790, line_y, balance, align="r")
        if i < len(desc):
            _text(c, 186, line_y, desc[i])
        if i < len(name):
            _text(c, 300, line_y, name[i])
        if i < len(iban):
            _text(c, 366, line_y, iban[i])
        if i == 0 and bank:
            _text(c, 434, line_y, bank)
    return y - depth * LEADING - ROW_GAP


def build(path: str) -> str:
    width, height = A4[1], A4[0]  # landscape
    c = canvas.Canvas(path, pagesize=(width, height))

    y = _page_header(c, width, height, first_page=True)
    # Row index 7 is deliberately split across the page break.
    for index, row in enumerate(ROWS):
        if index == SPLIT_ROW:
            # Only the first two description lines fit before the page break.
            y = _draw_row(c, y, row, max_desc_lines=SPLIT_AFTER)
            break
        y = _draw_row(c, y, row)

    c.showPage()
    y = _page_header(c, width, height, first_page=False)
    # Tail of the split row, then the remaining rows.
    tail = ROWS[SPLIT_ROW][3][SPLIT_AFTER:]
    for line_index, text in enumerate(tail):
        _text(c, 186, y - line_index * LEADING, text)
    y -= len(tail) * LEADING + ROW_GAP
    for row in ROWS[SPLIT_ROW + 1:]:
        y = _draw_row(c, y, row)

    y -= 14
    _text(c, 30, y, "Total DR Transactions", font="Helvetica-Bold", size=8)
    _text(c, 260, y, str(TOTALS["dr_count"]), align="r", font="Helvetica-Bold", size=8)
    _text(c, 30, y - 12, "Total CR Transactions", font="Helvetica-Bold", size=8)
    _text(c, 260, y - 12, str(TOTALS["cr_count"]), align="r", font="Helvetica-Bold", size=8)
    _text(c, 560, y, "Available Balance:", font="Helvetica-Bold", size=8)
    _text(c, 830, y, TOTALS["closing"], align="r", font="Helvetica-Bold", size=8)
    _text(c, 30, y - 30, "Sum of DR Transactions", font="Helvetica-Bold", size=8)
    _text(c, 260, y - 30, TOTALS["sum_dr"], align="r", font="Helvetica-Bold", size=8)
    _text(c, 30, y - 42, "Sum of CR Transactions", font="Helvetica-Bold", size=8)
    _text(c, 260, y - 42, TOTALS["sum_cr"], align="r", font="Helvetica-Bold", size=8)
    _text(c, 560, y - 30, "Closing Ledger Balance", font="Helvetica-Bold", size=8)
    _text(c, 830, y - 30, TOTALS["closing"], align="r", font="Helvetica-Bold", size=8)

    c.showPage()
    c.save()
    return path


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else "samples/mcb_fixture.pdf"))
