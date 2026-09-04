/**
 * Monal x IMARAT Dashboard — Google Apps Script
 *
 * Features:
 * 1. Auto-timestamp on data edits
 * 2. Custom menu for month expansion
 * 3. Auto-expand Total column when new months are added
 * 4. Optional email notifications on data changes
 *
 * Setup:
 *   Open Google Sheet → Extensions → Apps Script → paste this → Save.
 *
 *   Timestamps only:
 *     Nothing else to do. The simple onEdit trigger fires automatically.
 *
 *   Timestamps + email notifications:
 *     1. Set CONFIG.NOTIFY_EMAIL below.
 *     2. Set CONFIG.USE_INSTALLABLE_TRIGGER to true (this disables the simple
 *        trigger so the timestamp is not written twice per edit).
 *     3. Triggers → Add Trigger → onEditInstallable → From spreadsheet → On edit.
 *
 *   Recommended: use the menu item "Pin Timestamp Cell Here" once, so the
 *   timestamp follows its cell when columns are inserted. Without it the
 *   script falls back to the fixed CONFIG.TIMESTAMP_CELL address.
 */

// ===== CONFIG =====
const CONFIG = {
  SHEET_NAME: 'Actual P&L',
  TIMESTAMP_NAMED_RANGE: 'LastUpdated',  // preferred; survives column inserts
  TIMESTAMP_CELL: 'P1',                  // fallback if the named range is absent
  HEADER_ROW: 3,
  FIRST_DATA_ROW: 4,
  REF_COL: 2,                            // column B: row reference ("1", "4A", ...)
  DATA_START_COL: 3,                     // column C: first month column
  USE_INSTALLABLE_TRIGGER: false,        // true = simple onEdit stands down
  NOTIFY_EMAIL: '',
  DASHBOARD_URL: ''
};

// ===== TRIGGERS =====

/**
 * Simple trigger. Cannot send email (no authorization), so it only timestamps.
 * Stands down when the installable trigger is in use to avoid a double write.
 */
function onEdit(e) {
  if (!e || CONFIG.USE_INSTALLABLE_TRIGGER) return;  // e is undefined when run from the editor
  if (!isDataEdit_(e)) return;
  applyTimestamp_(e.source);
}

/**
 * Installable trigger. Install manually (see Setup) when you want email.
 */
function onEditInstallable(e) {
  if (!e || !isDataEdit_(e)) return;

  applyTimestamp_(e.source);

  if (!CONFIG.NOTIFY_EMAIL) return;
  try {
    MailApp.sendEmail({
      to: CONFIG.NOTIFY_EMAIL,
      subject: 'Monal Dashboard — ' + e.range.getA1Notation() + ' changed',
      body: buildNotificationBody_(e)
    });
  } catch (err) {
    // Quota exhausted or address rejected: log and move on. Throwing here would
    // fail the trigger run and, if it keeps failing, get the trigger disabled.
    console.error('Notification email failed: ' + err);
  }
}

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Monal Dashboard')
    .addItem('Expand Month (update Total formulas)', 'expandMonth')
    .addItem('Force Timestamp Update', 'forceTimestamp')
    .addItem('Pin Timestamp Cell Here', 'pinTimestampCell')
    .addSeparator()
    .addItem('Validate P&L Structure', 'validateStructure')
    .addToUi();
}

// ===== EDIT HANDLING =====

/**
 * True when the edited range overlaps the data area of the target sheet.
 * Uses getLast*() as well as getRow/getColumn: for a multi-cell paste the
 * top-left corner can sit outside the data area while the paste itself
 * lands inside it.
 */
function isDataEdit_(e) {
  const sheet = e.range.getSheet();
  if (sheet.getName() !== CONFIG.SHEET_NAME) return false;
  return e.range.getLastRow() >= CONFIG.FIRST_DATA_ROW &&
         e.range.getLastColumn() >= CONFIG.DATA_START_COL;
}

function applyTimestamp_(ss) {
  const range = getTimestampRange_(ss);
  if (!range) return;
  range.setValue('Last updated: ' + formatNow_(ss));
}

function getTimestampRange_(ss) {
  const named = ss.getRangeByName(CONFIG.TIMESTAMP_NAMED_RANGE);
  if (named) return named;
  const sheet = ss.getSheetByName(CONFIG.SHEET_NAME);
  return sheet ? sheet.getRange(CONFIG.TIMESTAMP_CELL) : null;
}

function formatNow_(ss) {
  // Spreadsheet timezone, not the script's — the two can differ.
  return Utilities.formatDate(new Date(), ss.getSpreadsheetTimeZone(), 'dd-MMM-yyyy HH:mm');
}

function buildNotificationBody_(e) {
  const cell = e.range.getA1Notation();
  const isSingleCell = e.range.getNumRows() === 1 && e.range.getNumColumns() === 1;

  // e.value / e.oldValue are only populated for single-cell edits.
  const detail = isSingleCell
    ? 'Old: ' + (e.oldValue === undefined ? '(empty)' : e.oldValue) + '\n' +
      'New: ' + (e.value === undefined ? '(empty)' : e.value) + '\n'
    : 'Multiple cells changed (' + e.range.getNumRows() + ' rows × ' +
      e.range.getNumColumns() + ' cols); before/after values are not available.\n';

  return 'Range ' + cell + ' on "' + e.range.getSheet().getName() + '" changed:\n\n' +
         detail + '\n' +
         'Sheet: ' + e.source.getUrl() + '\n' +
         (CONFIG.DASHBOARD_URL ? 'Dashboard: ' + CONFIG.DASHBOARD_URL : '');
}

// ===== EXPAND MONTH =====
function expandMonth() {
  const ui = SpreadsheetApp.getUi();
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(CONFIG.SHEET_NAME);
  if (!sheet) {
    ui.alert('Sheet "' + CONFIG.SHEET_NAME + '" not found. Check CONFIG.SHEET_NAME.');
    return;
  }

  const lastRow = sheet.getLastRow();
  const lastCol = sheet.getLastColumn();
  if (lastRow < CONFIG.FIRST_DATA_ROW || lastCol < CONFIG.DATA_START_COL) {
    ui.alert('Nothing to expand: the sheet has no data rows or no month columns yet.');
    return;
  }

  const headers = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, lastCol).getValues()[0];
  const totalCol = findTotalColumn_(headers);
  if (totalCol < 0) {
    ui.alert('Could not find the Total column in row ' + CONFIG.HEADER_ROW + '.\n\n' +
             'Expected a header like "Total (12M)" or "Total FY25".');
    return;
  }

  const firstMonthCol = CONFIG.DATA_START_COL;
  const lastMonthCol = totalCol - 1;
  const monthCount = lastMonthCol - firstMonthCol + 1;
  if (monthCount < 1) {
    ui.alert('The Total column (' + colLetter_(totalCol) + ') sits at or before the first ' +
             'month column (' + colLetter_(firstMonthCol) + '), so there is nothing to sum.');
    return;
  }

  const rowCount = lastRow - CONFIG.FIRST_DATA_ROW + 1;
  const labels = sheet.getRange(CONFIG.FIRST_DATA_ROW, 1, rowCount, CONFIG.REF_COL).getValues();
  const months = sheet.getRange(CONFIG.FIRST_DATA_ROW, firstMonthCol, rowCount, monthCount).getValues();
  const totalRange = sheet.getRange(CONFIG.FIRST_DATA_ROW, totalCol, rowCount, 1);
  const totalFormulas = totalRange.getFormulas();
  const totalValues = totalRange.getValues();

  let updated = 0;
  let preserved = 0;
  const out = [];

  for (let i = 0; i < rowCount; i++) {
    // Keep whatever is there unless we decide to replace it. getValues() flattens
    // formulas to their result, so fall back to the formula text when present.
    const existing = totalFormulas[i][0] || totalValues[i][0];
    const hasLabel = String(labels[i][0] || '').trim() || String(labels[i][1] || '').trim();
    const hasNumbers = months[i].some(v => v !== '' && v !== null);

    if (!hasLabel || !hasNumbers) {
      out.push([existing]);            // section headers, spacers, notes
      continue;
    }
    if (totalFormulas[i][0] && !/^=SUM\(/i.test(totalFormulas[i][0])) {
      out.push([existing]);            // hand-written formula: don't clobber it
      preserved++;
      continue;
    }

    const r = CONFIG.FIRST_DATA_ROW + i;
    out.push(['=SUM(' + colLetter_(firstMonthCol) + r + ':' + colLetter_(lastMonthCol) + r + ')']);
    updated++;
  }

  // One write instead of one per row: setValues() treats a leading "=" as a formula.
  totalRange.setValues(out);

  sheet.getRange(CONFIG.HEADER_ROW, totalCol).setValue('Total (' + monthCount + 'M)');
  sheet.getRange(CONFIG.FIRST_DATA_ROW, firstMonthCol, rowCount, monthCount + 1)
       .setNumberFormat('#,##0');

  ui.alert(
    'Done! ' + monthCount + ' months detected (' +
    colLetter_(firstMonthCol) + '–' + colLetter_(lastMonthCol) + ').\n' +
    'Total column ' + colLetter_(totalCol) + ': ' + updated + ' rows updated' +
    (preserved ? ', ' + preserved + ' custom formulas left untouched' : '') + '.\n\n' +
    'Dashboard will pick up the new month on next refresh.'
  );
}

/** Returns the 1-based Total column, or -1. */
function findTotalColumn_(headers) {
  for (let i = 0; i < headers.length; i++) {
    const h = String(headers[i] || '').trim();
    if (/^total\b/i.test(h) && /\(\s*\d+\s*m\s*\)|fy/i.test(h)) return i + 1;
  }
  // Fall back to a bare "Total" header.
  for (let i = 0; i < headers.length; i++) {
    if (/^total$/i.test(String(headers[i] || '').trim())) return i + 1;
  }
  return -1;
}

/** 1 → A, 27 → AA */
function colLetter_(col) {
  let s = '';
  while (col > 0) {
    const rem = (col - 1) % 26;
    s = String.fromCharCode(65 + rem) + s;
    col = Math.floor((col - 1) / 26);
  }
  return s;
}

// ===== VALIDATE STRUCTURE =====
function validateStructure() {
  const ui = SpreadsheetApp.getUi();
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(CONFIG.SHEET_NAME);
  if (!sheet) {
    ui.alert('Sheet "' + CONFIG.SHEET_NAME + '" not found. Check CONFIG.SHEET_NAME.');
    return;
  }

  const data = sheet.getDataRange().getValues();
  const issues = [];

  const headerRow = data[CONFIG.HEADER_ROW - 1];
  if (!headerRow || !headerRow[0]) {
    ui.alert('Issues found:\n\n• Row ' + CONFIG.HEADER_ROW + ' header not found — ' +
             'cannot validate the rest of the structure.');
    return;
  }

  // Required total rows, by Ref in column B.
  const requiredRefs = ['1', '2', '3', '4A', '4B'];
  const foundRefs = new Set();
  data.forEach(row => {
    const ref = String(row[CONFIG.REF_COL - 1] || '').trim();
    if (ref) foundRefs.add(ref);
  });
  requiredRefs.forEach(r => {
    if (!foundRefs.has(r)) issues.push('Missing total row with Ref: ' + r);
  });

  // Month columns.
  const months = headerRow.filter(cell => {
    const s = String(cell || '').trim();
    return /^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i.test(s) && s.indexOf('%') === -1;
  });
  if (months.length === 0) {
    issues.push('No month columns detected in row ' + CONFIG.HEADER_ROW);
  }

  // Total column.
  const totalCol = findTotalColumn_(headerRow);
  if (totalCol < 0) {
    issues.push('No Total column detected in row ' + CONFIG.HEADER_ROW);
  } else if (totalCol <= CONFIG.DATA_START_COL) {
    issues.push('Total column (' + colLetter_(totalCol) + ') is left of the first month column');
  }

  // Timestamp cell.
  if (!SpreadsheetApp.getActiveSpreadsheet().getRangeByName(CONFIG.TIMESTAMP_NAMED_RANGE)) {
    issues.push('No "' + CONFIG.TIMESTAMP_NAMED_RANGE + '" named range — falling back to ' +
                CONFIG.TIMESTAMP_CELL + ', which shifts meaning when columns are inserted. ' +
                'Use "Pin Timestamp Cell Here" to fix.');
  }

  if (issues.length === 0) {
    ui.alert(
      'Structure OK!\n\n' +
      'Refs found: ' + naturalSort_([...foundRefs]).join(', ') + '\n' +
      'Months (' + months.length + '): ' + months.join(', ') + '\n' +
      'Total column: ' + colLetter_(totalCol) + '\n' +
      'Total rows: ' + data.length
    );
  } else {
    ui.alert('Issues found:\n\n• ' + issues.join('\n• '));
  }
}

/** Sorts "1, 2, 3, 4A, 10" rather than "1, 10, 2, 3, 4A". */
function naturalSort_(arr) {
  return arr.sort((a, b) => {
    const na = parseInt(a, 10);
    const nb = parseInt(b, 10);
    if (!isNaN(na) && !isNaN(nb) && na !== nb) return na - nb;
    return String(a).localeCompare(String(b));
  });
}

// ===== TIMESTAMP HELPERS =====
function forceTimestamp() {
  const ui = SpreadsheetApp.getUi();
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const range = getTimestampRange_(ss);
  if (!range) {
    ui.alert('Sheet "' + CONFIG.SHEET_NAME + '" not found. Check CONFIG.SHEET_NAME.');
    return;
  }
  const ts = formatNow_(ss);
  range.setValue('Last updated: ' + ts);
  ui.alert('Timestamp updated: ' + ts);
}

/**
 * Pins the timestamp to the currently selected cell via a named range, so it
 * keeps its place when months are inserted to its left.
 */
function pinTimestampCell() {
  const ui = SpreadsheetApp.getUi();
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const cell = ss.getActiveCell();

  ss.setNamedRange(CONFIG.TIMESTAMP_NAMED_RANGE, cell);
  cell.setValue('Last updated: ' + formatNow_(ss));

  ui.alert('Timestamp pinned to ' + cell.getSheet().getName() + '!' + cell.getA1Notation() + '.');
}
