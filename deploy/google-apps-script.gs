/**
 * Gravity Gate <-> Google Sheets bridge.
 *
 * Setup (5 minutes, no Google Cloud project needed):
 *  1. Open your roster spreadsheet. Extensions -> Apps Script.
 *  2. Paste this file, set SECRET below to any passphrase, save.
 *  3. Deploy -> New deployment -> type "Web app".
 *       Execute as: Me.  Who has access: Anyone.
 *  4. Copy the web app URL into Gravity Gate Admin -> Settings -> Sheets sync URL,
 *     and the same passphrase into "Sync secret".
 *
 * GET  ?secret=...  returns the roster tab (ROSTER_TAB, or the first tab) as rows.
 * POST {secret, tabs: {title: [[row], ...]}}  rewrites each named tab.
 * Gravity Gate only ever writes tabs whose names start with "Gravity Gate" or "Standings -",
 * so your roster tab is never overwritten.
 */
const SECRET = 'change-me';
const ROSTER_TAB = 'Roster';

function doGet(e) {
  if ((e.parameter.secret || '') !== SECRET) return json_({ ok: false, error: 'bad secret' });
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(ROSTER_TAB) || ss.getSheets()[0];
  return json_({ ok: true, tab: sheet.getName(), rows: sheet.getDataRange().getDisplayValues() });
}

function doPost(e) {
  const body = JSON.parse(e.postData.contents);
  if ((body.secret || '') !== SECRET) return json_({ ok: false, error: 'bad secret' });
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const written = [];
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    Object.keys(body.tabs || {}).forEach(function (title) {
      if (!/^(Gravity Gate|Standings - )/.test(title)) return;
      const rows = body.tabs[title];
      if (!rows.length) return;
      const width = Math.max.apply(null, rows.map(function (r) { return r.length; }));
      const grid = rows.map(function (r) { while (r.length < width) r.push(''); return r; });
      const sheet = ss.getSheetByName(title) || ss.insertSheet(title);
      sheet.clearContents();
      sheet.getRange(1, 1, grid.length, width).setValues(grid);
      sheet.getRange(1, 1, 1, width).setFontWeight('bold');
      sheet.setFrozenRows(1);
      written.push(title);
    });
  } finally {
    lock.releaseLock();
  }
  return json_({ ok: true, tabs: written, at: body.generated_at });
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
