/** Manual integration check. Lock protects live bot traffic; only scratch data is removed. */
function verifyDeployment() {
  const lock = LockService.getScriptLock();
  lock.waitLock(25000);
  const props = PropertiesService.getScriptProperties();
  const book = SpreadsheetApp.openById(props.getProperty('SPREADSHEET_ID'));
  const oldStarts = props.getProperty('SHEET_START_DATES');
  const stamp = Date.now();
  const key = 'telegram:0:0:' + stamp;
  const editKey = 'telegram:0:0:op:' + stamp;
  const periodKey = 'telegram:0:0:op:' + (stamp + 1);
  let scratch;
  let createdId;
  try {
    scratch = book.insertSheet('_bot_check_' + stamp);
    book.setActiveSheet(scratch);
    book.moveActiveSheet(book.getNumSheets());
    if (scratch.getMaxColumns() < 39) scratch.insertColumnsAfter(scratch.getMaxColumns(), 39 - scratch.getMaxColumns());
    const starts = JSON.parse(oldStarts || '{}');
    starts[String(scratch.getSheetId())] = '2026-01-01';
    props.setProperty('SHEET_START_DATES', JSON.stringify(starts));
    scratch.getRange('E4').setValue(new Date('2026-01-01T12:00:00Z')).setNumberFormat('yyyy-mm-dd');
    scratch.getRange(13, 9, 1, 31).setValues([Array.from({length: 31}, (_, i) => i + 1)]);
    scratch.getRange(14, 2, 3, 4).setValues([
      [1, 'Тест продукты', '', 'Супермаркет'],
      [2, 'Тест кафе', '', 'Кофе'],
      ['Итого за день', '', '', '']
    ]);
    scratch.getRange('I14').setFormula('=(100+50)/2');
    scratch.getRange('I15').setValue(10.25);
    scratch.getRange('I16').setFormula('=SUM(I14:I15)');
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
    function read() {
      SpreadsheetApp.flush();
      return Sheets.Spreadsheets.Values.get(book.getId(), "'" + scratch.getName() + "'!I14:I15", {
        valueRenderOption: 'UNFORMATTED_VALUE'
      }).values;
    }
    let values = read();
    if (values[0][0] !== 325.5 || values[1][0] !== 11.25) throw new Error('Wrong cell totals');
    const changed = {key: editKey, target_key: key, sheet_id: scratch.getSheetId(),
      revision: catalog.revision, version: 0, expenses: [request.expenses[1]]};
    amend_(book, scratch, changed);
    amend_(book, scratch, changed);
    values = read();
    if (values[0][0] !== 75 || values[1][0] !== 11.25) throw new Error('Wrong amendment totals');
    const summary = summary_(book, scratch, {revision: catalog.revision,
      dates: ['2026-01-01'], category_ids: []});
    if (summary.total_minor !== 8625) throw new Error('Wrong summary');
    const plan = period_(book, scratch, {reference_date: '2026-02-15'});
    const create = {key: periodKey, sheet_id: scratch.getSheetId(), revision: catalog.revision,
      reference_date: '2026-02-15', start: plan.start, end: plan.end};
    const created = createPeriod_(book, scratch, create);
    createdId = created.sheet_id;
    if (createPeriod_(book, scratch, create).sheet_id !== createdId) throw new Error('Duplicate period');
    SpreadsheetApp.flush();
    const newSheet = book.getSheets().find(s => s.getSheetId() === createdId);
    const newCatalog = catalog_(book, newSheet);
    if (newCatalog.dates.length !== 28 || newCatalog.dates[0] !== '2026-02-01') throw new Error('New period dates');
    const fresh = summary_(book, newSheet, {revision: newCatalog.revision,
      dates: newCatalog.dates, category_ids: []});
    if (fresh.total_minor !== 0 || !newSheet.getRange('I16').getFormula()) throw new Error('Template not cleared correctly');
    console.log('PASS: write, amend, undo delta, summary, atomic period clone, formulas, idempotency');
  } finally {
    // Recover a created sheet even if the network response was lost after commit.
    const journal = book.getSheetByName(JOURNAL);
    if (!createdId && journal) {
      const created = entry_(journal, periodKey);
      if (created) createdId = created.receipt.sheet_id;
    }
    if (createdId) {
      const created = book.getSheets().find(s => s.getSheetId() === createdId);
      if (created) book.deleteSheet(created);
    }
    if (scratch) book.deleteSheet(scratch);
    if (journal) {
      [key, editKey, periodKey].forEach(k => {
        const found = journal.getRange('A:A').createTextFinder(k).matchEntireCell(true).findNext();
        if (found) journal.deleteRow(found.getRow());
      });
    }
    if (oldStarts === null) props.deleteProperty('SHEET_START_DATES');
    else props.setProperty('SHEET_START_DATES', oldStarts);
    lock.releaseLock();
  }
}
