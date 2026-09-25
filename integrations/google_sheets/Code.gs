/**
 * Google Sheets bridge for the Telegram expense bot.
 * Script properties: SPREADSHEET_ID, BRIDGE_SECRET (32+ chars), optionally
 * SHEET_START_DATES = {"182481067":"2026-08-10"}.
 * Enable the advanced Google Sheets API service before deployment.
 */
const JOURNAL = '_telegram_expenses';

function response_(body) {
  return ContentService.createTextOutput(JSON.stringify(body))
    .setMimeType(ContentService.MimeType.JSON);
}

function fail_(message) { const e = new Error(message); e.userFacing = true; throw e; }

function doPost(e) {
  let lock;
  try {
    const p = PropertiesService.getScriptProperties();
    const secret = p.getProperty('BRIDGE_SECRET');
    const input = JSON.parse(e.postData.contents);
    if (!secret || secret.length < 32 || input.secret !== secret) {
      return response_({ok: false, error: 'Нет доступа к таблице.'});
    }
    lock = LockService.getScriptLock();
    if (!lock.tryLock(25000)) {
      return response_({ok: false, error: 'Таблица занята.', retryable: true});
    }
    const book = SpreadsheetApp.openById(p.getProperty('SPREADSHEET_ID'));
    let result;
    if (input.action === 'sheets') {
      result = {sheets: book.getSheets().filter(s => !s.isSheetHidden() && s.getName() !== JOURNAL)
        .map(s => ({id: s.getSheetId(), title: s.getName()}))};
    } else {
      const sheet = book.getSheets().find(s => s.getSheetId() === input.sheet_id);
      if (!sheet || sheet.isSheetHidden() || sheet.getName() === JOURNAL) {
        fail_('Лист недоступен. Проверьте последний лист таблицы и повторите расход.');
      }
      if (input.action === 'catalog') result = publicCatalog_(catalog_(book, sheet));
      else if (input.action === 'write') result = write_(book, sheet, input);
      else fail_('Неизвестное действие.');
    }
    return response_({ok: true, result: result});
  } catch (error) {
    return response_({ok: false, retryable: !error.userFacing,
      error: error.userFacing ? error.message : 'Ошибка Google Sheets. Запрос будет повторён.'});
  } finally {
    if (lock && lock.hasLock()) lock.releaseLock();
  }
}

function hash_(text) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, text, Utilities.Charset.UTF_8)
    .map(b => ('0' + ((b + 256) % 256).toString(16)).slice(-2)).join('');
}

function isoDate_(year, month, day) {
  const d = new Date(Date.UTC(year, month - 1, day));
  if (d.getUTCFullYear() !== year || d.getUTCMonth() !== month - 1 || d.getUTCDate() !== day) {
    return null;
  }
  return d.toISOString().slice(0, 10);
}

function catalog_(book, sheet) {
  const props = PropertiesService.getScriptProperties();
  const starts = JSON.parse(props.getProperty('SHEET_START_DATES') || '{}');
  let start = starts[String(sheet.getSheetId())];
  if (!start) {
    const anchor = sheet.getRange('E4').getValue();
    if (!(anchor instanceof Date) || isNaN(anchor.getTime())) {
      fail_('Укажите дату начала этого листа в SHEET_START_DATES.');
    }
    start = Utilities.formatDate(anchor, book.getSpreadsheetTimeZone(), 'yyyy-MM-dd');
    const title = sheet.getName().match(/^(\d{2})\.(\d{2})\s*[-–]\s*(\d{2})\.(\d{2})$/);
    if (!title || Number(title[1]) !== Number(start.slice(8, 10)) ||
        Number(title[2]) !== Number(start.slice(5, 7))) {
      fail_('Название листа и дата E4 не совпадают. Укажите начало в SHEET_START_DATES.');
    }
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start)) fail_('Неверная дата начала листа.');
  let year = Number(start.slice(0, 4));
  let month = Number(start.slice(5, 7));
  const startDay = Number(start.slice(8, 10));
  if (isoDate_(year, month, startDay) !== start) fail_('Неверная дата начала листа.');
  const header = sheet.getRange(13, 9, 1, 31).getValues()[0];
  const columns = {};
  let previous = startDay;
  let rollovers = 0;
  header.forEach((value, index) => {
    if (value === '') return;
    const day = Number(value);
    if (!Number.isInteger(day) || day < 1 || day > 31) fail_('Неизвестный формат дней в I13:AM13.');
    if (index === 0 && day !== startDay) fail_('Первый день матрицы не совпадает с началом листа.');
    if (day < previous) {
      rollovers++;
      month++;
      if (month === 13) { month = 1; year++; }
    }
    if (rollovers > 1) fail_('Неоднозначная последовательность дней в листе.');
    previous = day;
    const iso = isoDate_(year, month, day);
    if (iso) {
      if (columns[iso]) fail_('Дата повторяется в заголовке листа.');
      columns[iso] = 9 + index;
    }
  });
  if (!Object.keys(columns).length) fail_('На листе не найдены дни расходов.');
  const values = sheet.getRange(14, 2, Math.max(1, sheet.getLastRow() - 13), 4).getValues();
  const categories = [];
  const rows = {};
  let group = '';
  let ended = false;
  for (let i = 0; i < values.length; i++) {
    const [number, name, unused, detail] = values[i];
    if (/^итого/i.test(String(number).trim())) { ended = true; break; }
    if (name !== '') group = String(name).trim();
    if (!group || (name === '' && detail === '')) continue;
    const label = group + (detail !== '' ? ' / ' + String(detail).trim() : '');
    const id = hash_(label).slice(0, 24);
    if (rows[id]) fail_('На листе повторяются названия категорий: ' + label);
    categories.push({id: id, label: label});
    rows[id] = 14 + i;
  }
  if (!ended || !categories.length) fail_('Не найдены категории и строка «Итого» на листе.');
  const revision = hash_(JSON.stringify({categories: categories, rows: rows, columns: columns}));
  return {id: sheet.getSheetId(), title: sheet.getName(), categories: categories,
    dates: Object.keys(columns), revision: revision, rows: rows, columns: columns};
}

function publicCatalog_(catalog) {
  return {id: catalog.id, title: catalog.title, categories: catalog.categories,
    dates: catalog.dates, revision: catalog.revision};
}

function journal_(book) {
  let sheet = book.getSheetByName(JOURNAL);
  if (!sheet) {
    sheet = book.insertSheet(JOURNAL);
    sheet.getRange(1, 1, 1, 3).setValues([['key', 'fingerprint', 'receipt']]);
    sheet.hideSheet();
    SpreadsheetApp.flush();
  }
  return sheet;
}

function write_(book, sheet, input) {
  if (typeof input.key !== 'string' || !/^telegram:\d+:\d+:\d+$/.test(input.key)) {
    fail_('Некорректный идентификатор сообщения.');
  }
  if (!Array.isArray(input.expenses) || !input.expenses.length || input.expenses.length > 20) {
    fail_('Не найдены расходы для записи.');
  }
  const fingerprint = hash_(JSON.stringify({sheet_id: input.sheet_id,
    revision: input.revision, expenses: input.expenses}));
  const journal = journal_(book);
  const found = journal.getRange('A:A').createTextFinder(input.key).matchEntireCell(true).findNext();
  if (found) {
    const row = journal.getRange(found.getRow(), 2, 1, 2).getValues()[0];
    if (row[0] !== fingerprint) fail_('Это сообщение уже записано. Повтор с другой суммой отклонён.');
    return JSON.parse(row[1]);
  }
  const latest = book.getSheets().filter(s => !s.isSheetHidden() && s.getName() !== JOURNAL).pop();
  if (!latest || latest.getSheetId() !== sheet.getSheetId()) {
    fail_('Последний лист изменился. Повторите расход — он попадёт в новый лист.');
  }
  const catalog = catalog_(book, sheet);
  if (catalog.revision !== input.revision) {
    fail_('Структура листа изменилась. Повторите расход.');
  }
  const totals = {};
  input.expenses.forEach(expense => {
    if (!Number.isSafeInteger(expense.amount_minor) || expense.amount_minor <= 0 ||
        expense.amount_minor > 100000000000) fail_('Неверная сумма расхода.');
    const row = catalog.rows[expense.category_id];
    const column = catalog.columns[expense.date];
    if (!row || !column) fail_('Категория или дата отсутствует на выбранном листе.');
    const key = row + ':' + column;
    if (!totals[key]) totals[key] = {row: row, column: column, minor: 0};
    totals[key].minor += expense.amount_minor;
  });
  const requests = [];
  Object.keys(totals).forEach(key => {
    const target = totals[key];
    const cell = sheet.getRange(target.row, target.column);
    const formula = cell.getFormula();
    const value = cell.getValue();
    if (value !== '' && (typeof value !== 'number' || !Number.isFinite(value))) {
      fail_('В ячейке расхода текст или ошибка. Исправьте её в таблице и повторите расход.');
    }
    const increment = '(' + target.minor + '/100)';
    let entered;
    if (formula) entered = {formulaValue: '=(' + formula.slice(1) + ')+' + increment};
    else entered = {numberValue: (Math.round(Number(value || 0) * 100) + target.minor) / 100};
    requests.push({updateCells: {
      range: {sheetId: sheet.getSheetId(), startRowIndex: target.row - 1, endRowIndex: target.row,
        startColumnIndex: target.column - 1, endColumnIndex: target.column},
      rows: [{values: [{userEnteredValue: entered}]}], fields: 'userEnteredValue'
    }});
  });
  const receipt = {sheet_id: sheet.getSheetId(), count: input.expenses.length,
    recorded_at: new Date().toISOString()};
  requests.push({appendCells: {sheetId: journal.getSheetId(), rows: [{values:
    [input.key, fingerprint, JSON.stringify(receipt)].map(v => ({userEnteredValue: {stringValue: v}}))
  }], fields: 'userEnteredValue'}});
  // Both the expense cells and its receipt succeed or fail in one atomic batch.
  Sheets.Spreadsheets.batchUpdate({requests: requests}, book.getId());
  return receipt;
}
