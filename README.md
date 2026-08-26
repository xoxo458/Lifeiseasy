# mcbx — MCB bank statement PDF → Excel

Replaces the manual job of retyping an MCB Bank account statement PDF into a
spreadsheet. Two ways to run it, both producing the same formatted `.xlsx` and
the same validation of the result.

## The browser app (no install, no cost)

Open **`mcb-statement-to-excel.html`** in any browser and drop a statement PDF
on it. A formatted workbook downloads.

- **Nothing to install** — no Python, no packages, no build step
- **Nothing leaves your machine** — no network requests at all; the PDF is read
  and the workbook written inside the page. Your bank data never travels.
- **No API key, no cost, ever**
- **Works offline** — save the file anywhere and open it, forever

It needs a text-based PDF (the original from internet banking), not a scan. Drop
a scan on it and it says so rather than guessing.

Rebuild it after changing `web/app.js` or `web/shell.html`:

```bash
./web/fetch-libs.sh        # once: downloads pdf.js and ExcelJS to be inlined
python3 web/build.py       # writes mcb-statement-to-excel.html
```

## The command line tool

For batches, folders, scripting, or scanned PDFs:

```bash
pip install -r requirements.txt
python -m mcbx statement.pdf                  # writes statement.xlsx
python -m mcbx statement.pdf -o Aug-2026.xlsx
python -m mcbx inbox/ -o converted/           # whole folder
```

Scanned statements need an API key (see [Scanned PDFs](#scanned-pdfs)):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Output format

Sheet **Statement** — rows 1–2 carry the account header, row 3 is the styled,
filtered, frozen column header, and transactions start at row 4:

| Column | Type | Notes |
|---|---|---|
| Tran. Date | date | real date, formatted `d-mmm-yyyy` |
| Effect Date | date | real date, formatted `dd-mmm-yy` |
| Tran. Br. | number | `0069` → `69` |
| Description | text | wrapped lines rejoined |
| Remitter Name | text | wrapped lines rejoined |
| Remitter IBAN | text | wrapped lines rejoined into the full IBAN |
| Remitter Bank | text | |
| Chq / Ref No | text | leading zeros preserved (`0000014203`) |
| Debit | number | `#,##0.00`; blank stays blank, never `0` |
| Credit | number | `#,##0.00` |
| Balance | number | `#,##0.00` |

Dates and amounts are written as real Excel values, not strings, so they sort,
filter and total correctly.

Sheet **Validation** — the extraction engine used, row count, the printed
opening/closing balances and footer totals, and every issue found.

### Rejoining wrapped cells

MCB wraps one transaction over several printed lines, breaking mid-word
(`FEDERAL EXCISE` / `DUTY TAX/`). The default `--wrap-join none` concatenates
with no separator, matching the required format (`FEDERAL EXCISEDUTY TAX/`) and
reassembling split IBANs correctly. Use `--wrap-join space` for readable prose
descriptions instead.

## Validation

Every conversion is checked before you use it — a plausible-looking wrong number
is the expensive failure, so the tool proves its work rather than trusting it:

- **Balance chain** — each row's balance must equal the previous balance plus
  credit minus debit, to the cent, starting from the printed opening balance.
- **Footer totals** — extracted debit/credit counts and sums must match the
  statement's own printed `Total DR/CR Transactions` and `Sum of DR/CR
  Transactions`, and the last row's balance must match the printed closing
  balance.
- **Per row** — a readable date, a balance, and exactly one of debit or credit.

Errors are printed and written to the Validation sheet. Add `--strict` to make
the command exit non-zero when anything fails, for unattended runs.

If a row is misread, the balance chain breaks at that row and the totals stop
reconciling — so a bad conversion announces itself instead of passing silently.

## Engines

`--engine auto` (default) picks per file:

- **text** — the PDF has a real text layer. Columns are located from the printed
  header row, so small layout shifts between statement runs are tolerated. Fast,
  free, deterministic, offline. This is what the browser app uses.
- **vision** — the PDF is a scan or photo with no text layer (a CamScanner
  export, for example). Pages are sent to Claude in small batches and returned as
  structured JSON. Costs a few cents per statement.

Force one with `--engine text`, `--engine ocr` or `--engine vision`.

### A note on scans, and why the text layer matters so much

A scanned statement is a photo of a printout: the bank generated a perfect PDF,
someone printed it, and the characters became pixels. Everything after that is
guesswork. **Get the original PDF from internet banking and none of this
applies** — the text engine reads the actual characters, free and instantly.

### Scanned PDFs

The vision engine needs `ANTHROPIC_API_KEY` in the environment. It sends page
groups of `--pages-per-call` (default 4) with `--concurrency` (default 3)
requests in flight. Claude is prompted to transcribe only — never to compute,
correct or infer a figure — and returns each wrapped cell as separate line
fragments, so rejoining stays deterministic in code rather than depending on the
model's formatting. Everything it returns is then checked by the same balance
chain and footer reconciliation as the text engine.

Tuning: `--effort high` (default) is right for statements; drop to `medium` for
clean scans, raise to `xhigh` for poor ones. If a batch hits the output limit,
lower `--pages-per-call`. Override the model with `MCBX_MODEL`.

### `--engine ocr`: free, offline, and only for clean scans

`--engine ocr` runs local Tesseract instead — no API, no cost, no network. It
needs the `tesseract` binary (`apt install tesseract-ocr`, `brew install
tesseract`).

**Know what you are getting.** On a crisp render it is accurate: the test suite
round-trips a generated statement through it with the balance chain and totals
reconciling. On a watermarked phone scan it is not. Measured against the real
CamScanner sample in this project's history:

| | Printed on statement | Tesseract read |
|---|---|---|
| Transactions | 52 | 21 |
| Sum of debits | 85,480,092.08 | 4,797,099.00 |
| Sum of credits | 77,919,000.00 | 106,150,000.00 |
| Balance chain | — | 12 broken rows |

It missed 60% of the rows and misread digits in most of the rest — the diagonal
"For Internal Use Only" watermark sits across the amount columns. The validation
layer caught every bit of it, which is the point, but caught is not converted.
Use `ocr` for clean scans, `vision` for photographs, and the text layer whenever
you can get it.

## Options

```
-o, --output PATH        .xlsx path (single input) or output directory
    --engine CHOICE      auto | text | ocr | vision      (default: auto)
    --wrap-join CHOICE   none | space                    (default: none)
    --ocr-scale N        ocr: page render multiplier     (default: 4.0)
    --pages-per-call N   vision: pages per request       (default: 4)
    --concurrency N      vision: parallel requests       (default: 3)
    --effort CHOICE      vision: low…max                 (default: high)
    --json PATH          also write the extracted data as JSON
    --strict             exit non-zero on validation errors
-q, --quiet              only report errors
```

## Layout assumptions

Tuned to the MCB "Account Statement" layout: the header block (Account No, IBAN,
Account Type / CCY, Statement Period, Opening Balance), the eleven-column
transaction table, and the footer totals block. Columns are found by their
printed header labels rather than fixed coordinates.

Two things to confirm against your own required format:

- **Column B** is labelled *Effect Date* (the statement's second date column). If
  your format wants a second copy of Tran. Date instead, change that entry in
  `COLUMNS` in `mcbx/excel.py`.
- **Other banks** are not supported. Adding one means a new engine module with
  its own column labels; the stitching, validation and Excel layers are
  bank-agnostic and would be reused as-is.

## Development

```bash
pip install -r requirements.txt pytest reportlab
python tests/make_fixture.py samples/mcb_fixture.pdf
python -m pytest tests/ -q
```

`tests/make_fixture.py` generates a digital statement in the MCB layout —
including wrapped cells, a row split across a page break, and a footer totals
block — so the pipeline is tested end to end without network access. The vision
engine's payload handling is tested against a recorded response shape; its
network call is not exercised in tests.

## Layout

```
mcb-statement-to-excel.html   the built browser app (open this)
web/
  app.js            browser port of the pipeline (pdf.js in, ExcelJS out)
  shell.html        page shell and UI
  build.py          inlines the libraries into one self-contained file
  fetch-libs.sh     downloads pdf.js and ExcelJS
mcbx/
  cli.py            argument parsing, batching, output paths
  engine_text.py    text-layer extraction (column detection, header/footer parsing)
  engine_ocr.py     offline Tesseract OCR (reuses the text engine's column logic)
  engine_vision.py  Claude transcription of scanned PDFs
  parse.py          folds wrapped lines into whole transactions
  models.py         Transaction / Statement, date and amount parsing
  validate.py       balance chain and footer reconciliation
  excel.py          the formatted workbook
```

All three Python engines emit the same `RawLine` shape, so stitching, validation
and Excel output are shared. The browser app is an independent port of the same
logic and is verified to produce byte-identical workbooks to the text engine.
