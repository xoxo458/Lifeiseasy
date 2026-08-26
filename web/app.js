/* mcbx web - MCB statement PDF to Excel, entirely in the browser.
 *
 * No network, no API, no upload: the PDF is read with pdf.js and the workbook
 * written with ExcelJS, both bundled into this page. Statement data never
 * leaves the machine.
 *
 * Mirrors the Python text engine: columns are located from the printed header
 * row, wrapped lines are stitched back into whole transactions, and every
 * conversion is checked against the statement's own balance chain and totals.
 */

const HEADER_LABELS = [
  ["Tran. Date", "tranDate"],
  ["Effect Date", "effectDate"],
  ["Tran. Br.", "branch"],
  ["Transaction Details", "description"],
  ["Remitter Name", "remitterName"],
  ["Remitter IBAN", "remitterIban"],
  ["Remitter Bank", "remitterBank"],
  ["Chq / Ref No", "refNo"],
  ["Debit", "debit"],
  ["Credit", "credit"],
  ["Balance", "balance"],
];

const TEXT_FIELDS = ["description", "remitterName", "remitterIban", "remitterBank", "refNo"];
const ATOMIC_FIELDS = ["debit", "credit", "balance"];
const LINE_TOLERANCE = 3.0;

const MONTHS = { JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5,
                 JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11 };

const NOISE_RE = new RegExp(
  "^(page\\s*[:\\d]|user\\s*id|note\\s*:|total\\s+(dr|cr)\\s+transactions|" +
  "sum\\s+of\\s+(dr|cr)\\s+transactions|available\\s+balance|closing\\s+ledger|" +
  "opening\\s+balance|account\\s+statement|camscanner|for\\s+internal\\s+use|" +
  "tran\\.?\\s*date|transaction\\s+details|mcb\\s+bank)", "i");

/* ---------- primitives ---------- */

function parseDate(raw) {
  if (!raw) return null;
  const m = /^\s*(\d{1,2})[-/\s]([A-Za-z]{3})[-/\s](\d{2}|\d{4})\s*$/.exec(raw);
  if (!m) return null;
  const mon = MONTHS[m[2].toUpperCase()];
  if (mon === undefined) return null;
  let year = parseInt(m[3], 10);
  if (m[3].length === 2) year += year < 80 ? 2000 : 1900;
  const day = parseInt(m[1], 10);
  const d = new Date(Date.UTC(year, mon, day));
  if (d.getUTCDate() !== day || d.getUTCMonth() !== mon) return null;
  return d;
}

/* Amounts are kept as integer cents so that summing many rows cannot drift. */
function parseAmount(raw) {
  if (raw === null || raw === undefined) return null;
  let text = String(raw).trim().replace(/,/g, "").replace(/\s/g, "");
  if (!text || text === "-" || text === "--" || text === ".") return null;
  let negative = false;
  if (text.startsWith("(") && text.endsWith(")")) { negative = true; text = text.slice(1, -1); }
  text = text.replace(/^\+/, "");
  if (text.startsWith("-")) { negative = true; text = text.slice(1); }
  if (!/^\d*\.?\d+$/.test(text)) return null;
  const [whole, frac = ""] = text.split(".");
  const cents = BigInt(whole || "0") * 100n + BigInt((frac + "00").slice(0, 2));
  return negative ? -cents : cents;
}

const centsToNumber = (c) => (c === null ? null : Number(c) / 100);
const formatCents = (c) =>
  (c === null ? "" : (Number(c) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

/* ---------- pdf.js text extraction ---------- */

async function pageWords(page) {
  const content = await page.getTextContent();
  const words = [];
  for (const item of content.items) {
    if (!item.str || !item.str.trim()) continue;
    const x = item.transform[4];
    const y = item.transform[5];
    const height = item.height || 8;
    // pdf.js emits a text run per drawn string, which may hold several table
    // cells. Split into tokens and place each by its character offset.
    const charWidth = item.width / Math.max(item.str.length, 1);
    const re = /\S+/g;
    let m;
    while ((m = re.exec(item.str)) !== null) {
      const x0 = x + m.index * charWidth;
      words.push({
        text: m[0],
        x0,
        x1: x0 + m[0].length * charWidth,
        top: -y,          // negate so ascending sort runs down the page
        bottom: -y + height,
      });
    }
  }
  return words;
}

function rowGroups(words) {
  const rows = [];
  const sorted = words.slice().sort((a, b) => (a.top - b.top) || (a.x0 - b.x0));
  for (const word of sorted) {
    const last = rows[rows.length - 1];
    if (last && Math.abs(word.top - last[0].top) <= LINE_TOLERANCE) last.push(word);
    else rows.push([word]);
  }
  for (const row of rows) row.sort((a, b) => a.x0 - b.x0);
  return rows;
}

function findLabel(row, label) {
  const tokens = label.split(/\s+/);
  const norm = (s) => s.replace(/\.+$/, "").toLowerCase();
  for (let i = 0; i + tokens.length <= row.length; i++) {
    let hit = true;
    for (let j = 0; j < tokens.length; j++) {
      if (norm(row[i + j].text) !== norm(tokens[j])) { hit = false; break; }
    }
    if (hit) return [row[i].x0, row[i + tokens.length - 1].x1];
  }
  return null;
}

function isHeaderRow(row) {
  const text = row.map((w) => w.text).join(" ");
  return text.includes("Balance") && text.includes("Debit") && text.includes("Date");
}

/* Column x-ranges, taken from the printed header row so that small layout
 * shifts between statement runs do not need code changes. */
function columnBounds(words) {
  for (const row of rowGroups(words)) {
    if (!isHeaderRow(row)) continue;
    const spans = [];
    for (const [label, field] of HEADER_LABELS) {
      const span = findLabel(row, label);
      if (span) spans.push({ field, x0: span[0], x1: span[1] });
    }
    if (spans.length < 6) continue;
    spans.sort((a, b) => a.x0 - b.x0);
    return spans.map((s, i) => ({
      field: s.field,
      left: i === 0 ? -Infinity : (spans[i - 1].x1 + s.x0) / 2,
      right: i === spans.length - 1 ? Infinity : (s.x1 + spans[i + 1].x0) / 2,
    }));
  }
  return null;
}

function headerBottom(words) {
  for (const row of rowGroups(words)) {
    if (isHeaderRow(row)) return Math.max(...row.map((w) => w.bottom));
  }
  return -Infinity;
}

function pageLines(words, bounds, pageNo) {
  const limit = headerBottom(words);
  const lines = [];
  for (const row of rowGroups(words)) {
    if (row[0].top <= limit) continue;
    const cells = {};
    for (const word of row) {
      const centre = (word.x0 + word.x1) / 2;
      const col = bounds.find((b) => centre >= b.left && centre < b.right);
      if (!col) continue;
      cells[col.field] = cells[col.field] ? cells[col.field] + " " + word.text : word.text;
    }
    if (Object.keys(cells).length) lines.push({ cells, page: pageNo });
  }
  return lines;
}

/* ---------- stitching ---------- */

const cellOf = (line, field) => (line.cells[field] || "").trim();

function isNoise(line) {
  const all = HEADER_LABELS.map(([, f]) => cellOf(line, f)).join(" ").trim();
  if (!all) return true;
  return NOISE_RE.test(all) || NOISE_RE.test(cellOf(line, "description"));
}

function cleanBranch(raw) {
  const text = (raw || "").trim();
  return /^\d+$/.test(text) ? String(parseInt(text, 10)) : text;
}

function stitch(lines, wrapJoin = "none") {
  const sep = wrapJoin === "space" ? " " : "";
  const out = [];

  for (const line of lines) {
    if (isNoise(line)) continue;
    const startsRow = parseDate(cellOf(line, "tranDate")) !== null;

    if (startsRow) {
      out.push({
        tranDate: parseDate(cellOf(line, "tranDate")),
        effectDate: parseDate(cellOf(line, "effectDate")) || parseDate(cellOf(line, "tranDate")),
        branch: cleanBranch(cellOf(line, "branch")),
        description: cellOf(line, "description"),
        remitterName: cellOf(line, "remitterName"),
        remitterIban: cellOf(line, "remitterIban"),
        remitterBank: cellOf(line, "remitterBank"),
        refNo: cellOf(line, "refNo"),
        debit: parseAmount(cellOf(line, "debit")),
        credit: parseAmount(cellOf(line, "credit")),
        balance: parseAmount(cellOf(line, "balance")),
        page: line.page,
      });
      continue;
    }

    // A continuation with no open transaction (a tail whose parent row was
    // never captured) is dropped rather than becoming a dateless row.
    if (!out.length) continue;

    const txn = out[out.length - 1];
    for (const field of TEXT_FIELDS) {
      const extra = cellOf(line, field);
      if (!extra) continue;
      txn[field] = txn[field] ? txn[field] + sep + extra : extra;
    }
    for (const field of ATOMIC_FIELDS) {
      if (txn[field] === null) {
        const value = parseAmount(cellOf(line, field));
        if (value !== null) txn[field] = value;
      }
    }
    if (!txn.branch) txn.branch = cleanBranch(cellOf(line, "branch"));
  }

  return out.filter((t) => t.tranDate || t.description || t.debit !== null || t.credit !== null);
}

/* ---------- header / footer blocks ---------- */

/* MCB prints two label/value pairs side by side:
 *
 *   Total DR Transactions    125        Available Balance:   3,563,090.00
 *
 * Flattened to text, both numbers follow the first label, so a regex grabs
 * whichever comes first. Instead, match the label in a visual line and take the
 * first number to its right, stopping at the next label. */
const FOOTER_FIELDS = [
  { label: "Total DR Transactions",  field: "totalDrCount",     kind: "count"  },
  { label: "Total CR Transactions",  field: "totalCrCount",     kind: "count"  },
  { label: "Sum of DR Transactions", field: "sumDr",            kind: "amount" },
  { label: "Sum of CR Transactions", field: "sumCr",            kind: "amount" },
  { label: "Available Balance",      field: "availableBalance", kind: "amount" },
  { label: "Closing Ledger Balance", field: "closingBalance",   kind: "amount" },
  { label: "Opening Balance",        field: "openingBalance",   kind: "amount" },
];

const LABEL_STARTS = FOOTER_FIELDS.map((f) => f.label.split(/\s+/).map(normToken));

function normToken(t) { return t.replace(/[.:]+$/, "").toLowerCase(); }

function labelAt(tokens, i, labelTokens) {
  if (i + labelTokens.length > tokens.length) return false;
  for (let j = 0; j < labelTokens.length; j++) {
    if (normToken(tokens[i + j]) !== labelTokens[j]) return false;
  }
  return true;
}

const startsAnyLabel = (tokens, i) => LABEL_STARTS.some((l) => labelAt(tokens, i, l));

/* A count is a bare integer; an amount carries MCB's two decimal places. That
 * distinction stops a balance being read as a transaction count. */
const COUNT_RE  = /^\d[\d,]*$/;
const AMOUNT_RE = /^\d[\d,]*\.\d{2}$/;

function valueAfterLabel(tokens, start, kind) {
  const re = kind === "count" ? COUNT_RE : AMOUNT_RE;
  for (let i = start; i < tokens.length && i < start + 5; i++) {
    if (startsAnyLabel(tokens, i)) return null;   // next pair begins; ours is blank
    if (re.test(tokens[i])) return tokens[i];
  }
  return null;
}

/* Scan every visual line for the footer/summary labels. */
function scanLabelledValues(rows) {
  const found = {};
  for (const tokens of rows) {
    for (const spec of FOOTER_FIELDS) {
      if (found[spec.field] !== undefined) continue;
      const labelTokens = spec.label.split(/\s+/).map(normToken);
      for (let i = 0; i < tokens.length; i++) {
        if (!labelAt(tokens, i, labelTokens)) continue;
        const value = valueAfterLabel(tokens, i + labelTokens.length, spec.kind);
        if (value !== null) found[spec.field] = value;
        break;
      }
    }
  }
  return found;
}

const grab = (re, text) => { const m = re.exec(text); return m ? m[1].trim() : ""; };

function parseMeta(text, rows) {
  const scanned = scanLabelledValues(rows);
  const title = (() => {
    const lines = text.split("\n").map((l) => l.trim()).filter(Boolean).slice(0, 6);
    const hit = lines.find((l) => /LIMITED/i.test(l) && !/MCB BANK/i.test(l)) || "";
    const cut = /\s*(Account\s*No|IBAN|Account\s*Type|Date of Account Open|Statement\s*(Period|Date))\b/i.exec(hit);
    return (cut ? hit.slice(0, cut.index) : hit).trim();
  })();

  return {
    accountTitle: title,
    accountNo: grab(/Account\s*No\.?\s*:?\s*([0-9]{6,})/i, text),
    iban: grab(/IBAN\s*:?\s*([A-Z]{2}[0-9A-Z]{10,32})/i, text),
    accountType: grab(/Account\s*Type\s*\/?\s*CCY\s*:?\s*([A-Z]+)\s*\/\s*[A-Z]{3}/i, text),
    currency: grab(/Account\s*Type\s*\/?\s*CCY\s*:?\s*[A-Z]+\s*\/\s*([A-Z]{3})/i, text),
    branch: grab(/(\d{3,4}-[A-Z][A-Z ]+)/, text),
    periodFrom: parseDate(grab(/From Date\s*:?\s*([0-9]{1,2}-[A-Za-z]{3}-[0-9]{2,4})/i, text)),
    periodTo: parseDate(grab(/To Date\s*:?\s*([0-9]{1,2}-[A-Za-z]{3}-[0-9]{2,4})/i, text)),
    statementDateTime: grab(/Statement Date & Time\s*:?\s*(.+)/i, text),
    openingBalance: parseAmount(scanned.openingBalance),
  };
}

function parseTotals(rows) {
  const scanned = scanLabelledValues(rows);
  const count = (v) => {
    if (v === undefined || v === null) return null;
    const n = parseInt(String(v).replace(/,/g, ""), 10);
    return Number.isFinite(n) ? n : null;
  };
  return {
    totalDrCount: count(scanned.totalDrCount),
    totalCrCount: count(scanned.totalCrCount),
    sumDr: parseAmount(scanned.sumDr),
    sumCr: parseAmount(scanned.sumCr),
    availableBalance: parseAmount(scanned.availableBalance),
    closingBalance: parseAmount(scanned.closingBalance),
  };
}

/* ---------- validation ---------- */

function validate(statement) {
  const issues = [];
  const txns = statement.transactions;
  const add = (severity, row, check, detail) => issues.push({ severity, row, check, detail });

  txns.forEach((txn, i) => {
    const n = i + 1;
    if (!txn.tranDate) add("error", n, "date", "row has no readable transaction date");
    if (txn.debit === null && txn.credit === null) add("error", n, "amount", "row has neither a debit nor a credit");
    if (txn.debit !== null && txn.credit !== null)
      add("error", n, "amount", `row has both a debit (${formatCents(txn.debit)}) and a credit (${formatCents(txn.credit)})`);
    if (txn.balance === null) add("error", n, "balance", "row has no readable balance");
    if (!txn.description) add("warning", n, "description", "row has an empty description");
  });

  // Balance chain: each balance must follow from the one above it.
  let chainIssues = 0;
  let previous = statement.meta.openingBalance;
  txns.forEach((txn, i) => {
    if (txn.balance === null) { previous = null; return; }
    if (previous !== null) {
      const move = (txn.credit || 0n) - (txn.debit || 0n);
      const expected = previous + move;
      if (expected !== txn.balance) {
        chainIssues++;
        add("error", i + 1, "balance-chain",
            `expected ${formatCents(expected)} from ${formatCents(previous)} ` +
            `${move >= 0n ? "+" : "-"} ${formatCents(move < 0n ? -move : move)}, ` +
            `statement shows ${formatCents(txn.balance)} (off by ${formatCents(txn.balance - expected)})`);
      }
    }
    previous = txn.balance;
  });

  // Footer reconciliation against the statement's own printed totals.
  let totalIssues = 0;
  const t = statement.totals;
  const drRows = txns.filter((x) => x.debit !== null);
  const crRows = txns.filter((x) => x.credit !== null);
  const sumDr = drRows.reduce((a, x) => a + x.debit, 0n);
  const sumCr = crRows.reduce((a, x) => a + x.credit, 0n);

  const compare = (label, extracted, printed, check, fmt) => {
    if (printed === null || printed === undefined) {
      add("warning", null, check, `statement does not print ${label}; not reconciled`);
    } else if (extracted !== printed) {
      totalIssues++;
      add("error", null, check, `${label}: extracted ${fmt(extracted)}, statement prints ${fmt(printed)}`);
    }
  };
  const plain = (v) => String(v);
  compare("debit count", drRows.length, t.totalDrCount, "count-dr", plain);
  compare("credit count", crRows.length, t.totalCrCount, "count-cr", plain);
  compare("sum of debits", sumDr, t.sumDr, "sum-dr", formatCents);
  compare("sum of credits", sumCr, t.sumCr, "sum-cr", formatCents);

  if (txns.length && txns[txns.length - 1].balance !== null && t.closingBalance !== null) {
    const last = txns[txns.length - 1].balance;
    if (last !== t.closingBalance) {
      totalIssues++;
      add("error", null, "closing-balance",
          `last row balance ${formatCents(last)} != printed closing balance ${formatCents(t.closingBalance)}`);
    }
  }

  return {
    issues,
    checkedRows: txns.length,
    balanceChainOk: chainIssues === 0,
    totalsOk: totalIssues === 0,
    errors: issues.filter((i) => i.severity === "error"),
    warnings: issues.filter((i) => i.severity === "warning"),
  };
}

/* ---------- conversion ---------- */

async function convertPdf(arrayBuffer, options = {}) {
  const wrapJoin = options.wrapJoin || "none";
  const onProgress = options.onProgress || (() => {});

  const pdf = await pdfjsLib.getDocument({ data: arrayBuffer }).promise;
  const lines = [];
  const texts = [];
  const rows = [];   // every visual line as tokens, for the labelled-value scan

  for (let n = 1; n <= pdf.numPages; n++) {
    const page = await pdf.getPage(n);
    const words = await pageWords(page);
    const content = await page.getTextContent();
    texts.push(content.items.map((i) => i.str).join(" "));
    onProgress(n, pdf.numPages);
    if (!words.length) continue;
    rows.push(...rowGroups(words).map((r) => r.map((w) => w.text)));
    const bounds = columnBounds(words);
    if (!bounds) continue;
    lines.push(...pageLines(words, bounds, n));
  }

  if (!lines.length) {
    throw new Error(
      "No statement table found. This looks like a scanned PDF with no text " +
      "layer — ask the bank for the original PDF from internet banking.");
  }

  const fullText = texts.join("\n");
  const statement = {
    meta: parseMeta(fullText, rows),
    totals: parseTotals(rows),
    transactions: stitch(lines, wrapJoin),
  };
  return { statement, report: validate(statement) };
}

/* ---------- workbook ---------- */

const COLUMNS = [
  ["Tran. Date",    "tranDate",     12, "d-mmm-yyyy", "left"],
  ["Effect Date",   "effectDate",   12, "dd-mmm-yy",  "left"],
  ["Tran. Br.",     "branch",        9, "0",          "left"],
  ["Description",   "description",  62, null,         "left"],
  ["Remitter Name", "remitterName", 22, null,         "left"],
  ["Remitter IBAN", "remitterIban", 26, null,         "left"],
  ["Remitter Bank", "remitterBank", 16, null,         "left"],
  ["Chq / Ref No",  "refNo",        14, "@",          "left"],
  ["Debit",         "debit",        16, "#,##0.00",   "right"],
  ["Credit",        "credit",       16, "#,##0.00",   "right"],
  ["Balance",       "balance",      18, "#,##0.00",   "right"],
];

const HEADER_ROW = 3;
const FIRST_DATA_ROW = HEADER_ROW + 1;
const BORDER = { top: { style: "thin", color: { argb: "FFD9D9D9" } },
                 left: { style: "thin", color: { argb: "FFD9D9D9" } },
                 bottom: { style: "thin", color: { argb: "FFD9D9D9" } },
                 right: { style: "thin", color: { argb: "FFD9D9D9" } } };

async function buildWorkbook(statement, report, sourceName) {
  const wb = new ExcelJS.Workbook();
  const ws = wb.addWorksheet("Statement");
  const meta = statement.meta;

  ws.columns = COLUMNS.map(([, , width]) => ({ width }));

  const fmtDate = (d) => d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
  const left = [meta.accountTitle, meta.branch].filter(Boolean).join(" | ");
  const right = [
    meta.accountNo ? `A/C No: ${meta.accountNo}` : "",
    meta.iban ? `IBAN: ${meta.iban}` : "",
    (meta.accountType || meta.currency) ? `${meta.accountType}/${meta.currency}` : "",
  ].filter(Boolean).join(" | ");

  ws.getCell(1, 1).value = left || "Account Statement";
  ws.getCell(1, 1).font = { bold: true, size: 11 };
  ws.getCell(1, 5).value = right;
  if (meta.periodFrom && meta.periodTo)
    ws.getCell(2, 1).value = `Statement Period: ${fmtDate(meta.periodFrom)} to ${fmtDate(meta.periodTo)}`;
  if (meta.openingBalance !== null) {
    ws.getCell(2, 9).value = "Opening Balance:";
    ws.getCell(2, 9).font = { bold: true, size: 11 };
    const cell = ws.getCell(2, 11);
    cell.value = centsToNumber(meta.openingBalance);
    cell.numFmt = "#,##0.00";
  }

  COLUMNS.forEach(([header], i) => {
    const cell = ws.getCell(HEADER_ROW, i + 1);
    cell.value = header;
    cell.fill = { type: "pattern", pattern: "solid", fgColor: { argb: "FF1F3864" } };
    cell.font = { color: { argb: "FFFFFFFF" }, bold: true, size: 11 };
    cell.alignment = { horizontal: "left", vertical: "middle" };
    cell.border = BORDER;
  });
  ws.getRow(HEADER_ROW).height = 20;

  statement.transactions.forEach((txn, r) => {
    const rowNo = FIRST_DATA_ROW + r;
    COLUMNS.forEach(([, field, , numFmt, align], c) => {
      const cell = ws.getCell(rowNo, c + 1);
      const raw = txn[field];
      if (raw !== null && raw !== undefined && raw !== "") {
        if (field === "branch") cell.value = /^\d+$/.test(raw) ? parseInt(raw, 10) : raw;
        else if (field === "refNo") cell.value = String(raw);          // text keeps leading zeros
        else if (typeof raw === "bigint") cell.value = centsToNumber(raw);
        else cell.value = raw;
      }
      if (numFmt) cell.numFmt = numFmt;
      cell.alignment = { horizontal: align, vertical: "top" };
      cell.border = BORDER;
    });
  });

  const lastRow = Math.max(FIRST_DATA_ROW + statement.transactions.length - 1, HEADER_ROW);
  ws.autoFilter = { from: { row: HEADER_ROW, column: 1 }, to: { row: lastRow, column: COLUMNS.length } };
  ws.views = [{ state: "frozen", ySplit: FIRST_DATA_ROW - 1 }];

  const vs = wb.addWorksheet("Validation");
  const t = statement.totals;
  const rows = [
    ["Source file", sourceName],
    ["Extraction engine", "browser (pdf.js text layer)"],
    ["Transactions extracted", report.checkedRows],
    ["Balance chain", report.balanceChainOk ? "OK" : "FAILED"],
    ["Footer totals", report.totalsOk ? "OK" : "FAILED"],
    ["Errors", report.errors.length],
    ["Warnings", report.warnings.length],
    ["", ""],
    ["Opening balance (printed)", centsToNumber(meta.openingBalance)],
    ["Closing balance (printed)", centsToNumber(t.closingBalance)],
    ["Available balance (printed)", centsToNumber(t.availableBalance)],
    ["Total DR transactions (printed)", t.totalDrCount],
    ["Total CR transactions (printed)", t.totalCrCount],
    ["Sum of DR transactions (printed)", centsToNumber(t.sumDr)],
    ["Sum of CR transactions (printed)", centsToNumber(t.sumCr)],
  ];
  rows.forEach(([label, value], i) => {
    vs.getCell(i + 1, 1).value = label;
    if (label) vs.getCell(i + 1, 1).font = { bold: true };
    vs.getCell(i + 1, 2).value = value === undefined ? null : value;
  });
  const start = rows.length + 2;
  ["Severity", "Row", "Check", "Detail"].forEach((h, c) => {
    const cell = vs.getCell(start, c + 1);
    cell.value = h;
    cell.fill = { type: "pattern", pattern: "solid", fgColor: { argb: "FF1F3864" } };
    cell.font = { color: { argb: "FFFFFFFF" }, bold: true };
  });
  report.issues.forEach((issue, i) => {
    vs.getCell(start + i + 1, 1).value = issue.severity;
    vs.getCell(start + i + 1, 2).value = issue.row;
    vs.getCell(start + i + 1, 3).value = issue.check;
    vs.getCell(start + i + 1, 4).value = issue.detail;
  });
  vs.columns = [{ width: 34 }, { width: 22 }, { width: 18 }, { width: 90 }];

  return wb.xlsx.writeBuffer();
}
