/** Manual deployment check. Creates and removes only its own temporary test sheet. */
function verifyDeployment() {
  const lock = LockService.getScriptLock();
  lock.waitLock(25000);
  const props = PropertiesService.getScriptProperties();
  const book = SpreadsheetApp.openById(props.getProperty('SPREADSHEET_ID'));
  const oldStarts = props.getProperty('SHEET_START_DATES');
  const key = 'telegram:0:0:' + Date.now();
  let scratch;
  try {
    scratch = book.insertSheet('_bot_check_' + Date.now());
    if (scratch.getMaxColumns() < 39) scratch.insertColumnsAfter(scratch.getMaxColumns(), 39 - scratch.getMaxColumns());
    const starts = JSON.parse(oldStarts || '{}');
    starts[String(scratch.getSheetId())] = '2026-01-01';
    props.setProperty('SHEET_START_DATES', JSON.stringify(starts));
    scratch.getRange(13, 9, 1, 31).setValues([Array.from({length: 31}, (_, i) => i + 1)]);
    scratch.getRange(14, 2, 3, 4).setValues([
      [1, 'Тест продукты', '', 'Супермаркет'],
      [2, 'Тест кафе', '', 'Кофе'],
      ['Итого за день', '', '', '']
    ]);
    scratch.getRange('I14').setFormula('=(100+50)/2');
    scratch.getRange('I15').setValue(10.25);
    SpreadsheetApp.flush();
    const catalog = catalog_(book, scratch);
    const request = {key: key, sheet_id: scratch.getSheetId(), revision: catalog.revision,
      expenses: [
        {category_id: catalog.categories[0].id, date: '2026-01-01', amount_minor: 25050, description: 'test'},
        {category_id: catalog.categories[1].id, date: '2026-01-01', amount_minor: 100, description: 'test'}
      ]};
    const first = write_(book, scratch, request);
    const second = write_(book, scratch, request);
    if (JSON.stringify(first) !== JSON.stringify(second)) throw new Error('Deduplication failed');
    const range = "'" + scratch.getName() + "'!I14:I15";
    const values = Sheets.Spreadsheets.Values.get(book.getId(), range, {
      valueRenderOption: 'UNFORMATTED_VALUE'
    }).values;
    if (values[0][0] !== 325.5 || values[1][0] !== 11.25) throw new Error('Wrong cell totals');
    console.log('PASS: atomic write, formula preservation, decimal amounts, duplicate prevention');
  } finally {
    if (scratch) book.deleteSheet(scratch);
    const journal = book.getSheetByName(JOURNAL);
    if (journal) {
      const found = journal.getRange('A:A').createTextFinder(key).matchEntireCell(true).findNext();
      if (found) journal.deleteRow(found.getRow());
    }
    if (oldStarts === null) props.deleteProperty('SHEET_START_DATES');
    else props.setProperty('SHEET_START_DATES', oldStarts);
    lock.releaseLock();
  }
}
