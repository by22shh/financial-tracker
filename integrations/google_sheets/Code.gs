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
      else if (input.action === 'amend') result = amend_(book, sheet, input);
      else if (input.action === 'summary') result = summary_(book, sheet, input);
      else if (input.action === 'category_status') result = categoryStatus_(book, sheet, input);
      else if (input.action === 'period') result = period_(book, sheet, input);
      else if (input.action === 'create_period') result = createPeriod_(book, sheet, input);
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
    const title = sheet.getName().match(/^(\d{2})\.(\d{2})\s*[-–]\s*(\d{2})\.(\d{2})(?: \d{4})?$/);
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


function validKey_(key) {
  if (typeof key !== 'string' || !/^telegram:\d+:\d+:(?:op:)?\d+$/.test(key)) {
    fail_('Некорректный идентификатор сообщения.');
  }
}

function entry_(journal, key) {
  const found = journal.getRange('A:A').createTextFinder(key).matchEntireCell(true).findNext();
  if (!found) return null;
  // Advanced API writes do not reliably invalidate SpreadsheetApp's read cache.
  const row = (Sheets.Spreadsheets.Values.get(journal.getParent().getId(),
    "'" + JOURNAL + "'!B" + found.getRow() + ':C' + found.getRow()).values || [])[0];
  if (!row || row.length < 2) throw new Error('Receipt not readable yet');
  return {row: found.getRow(), fingerprint: row[0], receipt: JSON.parse(row[1])};
}

function repeat_(journal, key, fingerprint) {
  const found = entry_(journal, key);
  if (!found) return null;
  if (found.fingerprint !== fingerprint) fail_('Это сообщение уже записано. Повтор с другой суммой отклонён.');
  return found.receipt;
}

function appendReceipt_(journal, key, fingerprint, receipt) {
  return {appendCells: {sheetId: journal.getSheetId(), rows: [{values:
    [key, fingerprint, JSON.stringify(receipt)].map(v => ({userEnteredValue: {stringValue: v}}))
  }], fields: 'userEnteredValue'}};
}

function latest_(book, sheet) {
  const latest = book.getSheets().filter(s => !s.isSheetHidden() && s.getName() !== JOURNAL).pop();
  if (!latest || latest.getSheetId() !== sheet.getSheetId()) {
    fail_('Последний лист изменился. Повторите запрос — он попадёт в новый лист.');
  }
}

function totals_(catalog, expenses, allowEmpty) {
  if (!Array.isArray(expenses) || (!allowEmpty && !expenses.length) || expenses.length > 20) {
    fail_('Не найдены расходы для записи.');
  }
  const totals = {};
  expenses.forEach(expense => {
    if (!Number.isSafeInteger(expense.amount_minor) || expense.amount_minor <= 0 ||
        expense.amount_minor > 100000000000) fail_('Неверная сумма расхода.');
    const row = catalog.rows[expense.category_id];
    const column = catalog.columns[expense.date];
    if (!row || !column) fail_('Категория или дата отсутствует на выбранном листе.');
    const key = row + ':' + column;
    if (!totals[key]) totals[key] = {row: row, column: column, minor: 0};
    totals[key].minor += expense.amount_minor;
  });
  return totals;
}

function cellUpdate_(sheetId, row, column, entered) {
  return {updateCells: {
    range: {sheetId: sheetId, startRowIndex: row - 1, endRowIndex: row,
      startColumnIndex: column - 1, endColumnIndex: column},
    rows: [{values: [{userEnteredValue: entered}]}], fields: 'userEnteredValue'
  }};
}

function changes_(sheet, totals) {
  return Object.keys(totals).filter(key => totals[key].minor !== 0).map(key => {
    const target = totals[key];
    const cell = sheet.getRange(target.row, target.column);
    const formula = cell.getFormula();
    const value = cell.getValue();
    if (value !== '' && (typeof value !== 'number' || !Number.isFinite(value))) {
      fail_('В ячейке расхода текст или ошибка. Исправьте её в таблице и повторите расход.');
    }
    const minor = Math.round(Number(value || 0) * 100) + target.minor;
    if (!Number.isSafeInteger(minor) || (target.minor < 0 && minor < 0)) {
      fail_('Сумма в таблице изменилась вручную. Проверьте ячейку перед исправлением.');
    }
    const increment = '(' + target.minor + '/100)';
    const entered = formula ? {formulaValue: '=(' + formula.slice(1) + ')+' + increment} :
      {numberValue: minor / 100};
    return cellUpdate_(sheet.getSheetId(), target.row, target.column, entered);
  });
}

function write_(book, sheet, input) {
  validKey_(input.key);
  // Keep the original fingerprint shape for retries from earlier bot versions.
  const fingerprint = hash_(JSON.stringify({sheet_id: input.sheet_id,
    revision: input.revision, expenses: input.expenses}));
  const journal = journal_(book);
  const repeated = repeat_(journal, input.key, fingerprint);
  if (repeated) return repeated;
  latest_(book, sheet);
  const catalog = catalog_(book, sheet);
  if (catalog.revision !== input.revision) fail_('Структура листа изменилась. Повторите расход.');
  const requests = changes_(sheet, totals_(catalog, input.expenses, false));
  const receipt = {sheet_id: sheet.getSheetId(), count: input.expenses.length,
    recorded_at: new Date().toISOString(), revision: catalog.revision,
    expenses: input.expenses, version: 0};
  requests.push(appendReceipt_(journal, input.key, fingerprint, receipt));
  Sheets.Spreadsheets.batchUpdate({requests: requests}, book.getId());
  return receipt;
}

function amend_(book, sheet, input) {
  validKey_(input.key); validKey_(input.target_key);
  if (input.key.split(':').slice(0, 3).join(':') !== input.target_key.split(':').slice(0, 3).join(':')) {
    fail_('Можно исправлять только свои расходы.');
  }
  const fingerprint = hash_(JSON.stringify({action: 'amend', sheet_id: input.sheet_id,
    target_key: input.target_key, version: input.version, revision: input.revision,
    expenses: input.expenses}));
  const journal = journal_(book);
  const repeated = repeat_(journal, input.key, fingerprint);
  if (repeated) return repeated;
  latest_(book, sheet);
  const catalog = catalog_(book, sheet);
  const original = entry_(journal, input.target_key);
  if (!original || !original.receipt.expenses || original.receipt.sheet_id !== catalog.id) {
    fail_('Для этой старой записи нет данных исправления. Измените её в таблице.');
  }
  if (catalog.revision !== input.revision || catalog.revision !== original.receipt.revision) {
    fail_('Структура листа изменилась. Исправьте запись в таблице.');
  }
  if (!Number.isInteger(input.version) || input.version !== original.receipt.version) {
    fail_('Запись уже изменилась. Используйте кнопки под новой квитанцией.');
  }
  const totals = totals_(catalog, input.expenses, true);
  const old = totals_(catalog, original.receipt.expenses, true);
  Object.keys(old).forEach(key => {
    if (!totals[key]) totals[key] = {...old[key], minor: 0};
    totals[key].minor -= old[key].minor;
  });
  const requests = changes_(sheet, totals);
  const receipt = {...original.receipt, count: input.expenses.length, expenses: input.expenses,
    version: input.version + 1, updated_at: new Date().toISOString()};
  requests.push(cellUpdate_(journal.getSheetId(), original.row, 3,
    {stringValue: JSON.stringify(receipt)}));
  requests.push(appendReceipt_(journal, input.key, fingerprint, receipt));
  Sheets.Spreadsheets.batchUpdate({requests: requests}, book.getId());
  return receipt;
}

function summary_(book, sheet, input) {
  latest_(book, sheet);
  const catalog = catalog_(book, sheet);
  if (catalog.revision !== input.revision) fail_('Структура листа изменилась. Повторите запрос.');
  if (!Array.isArray(input.dates) || !input.dates.length || input.dates.length > 31 ||
      input.dates.some(d => !catalog.columns[d])) fail_('Дата отсутствует на последнем листе.');
  if (!Array.isArray(input.category_ids) || input.category_ids.some(id => !catalog.rows[id])) {
    fail_('Категория отсутствует на последнем листе.');
  }
  const categories = catalog.categories.filter(c => !input.category_ids.length || input.category_ids.includes(c.id));
  const dates = [...new Set(input.dates)].sort();
  let total = 0;
  const rows = categories.map(category => {
    // Read each category as one range; include manual entries and evaluated formulas.
    const values = sheet.getRange(catalog.rows[category.id], 9, 1, 31).getValues()[0];
    let minor = 0;
    dates.forEach(d => {
      const value = values[catalog.columns[d] - 9];
      if (value !== '' && (typeof value !== 'number' || !Number.isFinite(value))) {
        fail_('В таблице есть текст или ошибка вместо суммы. Исправьте ячейку перед сводкой.');
      }
      minor += Math.round(Number(value || 0) * 100);
    });
    total += minor;
    return {id: category.id, label: category.label, amount_minor: minor};
  }).filter(c => c.amount_minor !== 0);
  if (!Number.isSafeInteger(total)) fail_('Слишком большая сумма для сводки.');
  return {title: catalog.title, from: dates[0], to: dates[dates.length - 1],
    categories: rows, total_minor: total};
}

function categoryStatus_(book, sheet, input) {
  const catalog = catalog_(book, sheet);
  if (catalog.revision !== input.revision) fail_('Структура листа изменилась.');
  if (!Array.isArray(input.category_ids) || !input.category_ids.length ||
      input.category_ids.length > 20 || input.category_ids.some(id => !catalog.rows[id])) {
    fail_('Категория отсутствует на листе.');
  }
  // This action runs after the atomic write, in a new Apps Script execution.
  // F is the sheet's calculated period spend; G is its manually maintained plan.
  const values = sheet.getRange(14, 6, sheet.getLastRow() - 13, 2).getValues();
  function minor(value, empty) {
    if (value === '' && empty) return null;
    if (value === '') return 0;
    if (typeof value !== 'number' || !Number.isFinite(value)) {
      fail_('В расходах или плане категории обнаружена ошибка.');
    }
    const result = Math.round(value * 100);
    if (!Number.isSafeInteger(result)) fail_('Слишком большая сумма категории.');
    return result;
  }
  return {categories: [...new Set(input.category_ids)].map(id => {
    const [spent, plan] = values[catalog.rows[id] - 14];
    return {id: id, spent_minor: minor(spent, false), plan_minor: minor(plan, true)};
  })};
}

function addDays_(iso, count) {
  const d = new Date(iso + 'T12:00:00Z');
  d.setUTCDate(d.getUTCDate() + count);
  return d.toISOString().slice(0, 10);
}

function monthStart_(iso) {
  const d = new Date(iso + 'T12:00:00Z');
  d.setUTCMonth(d.getUTCMonth() + 1);
  return d.toISOString().slice(0, 10);
}

function period_(book, sheet, input) {
  latest_(book, sheet);
  const catalog = catalog_(book, sheet);
  const reference = input.reference_date;
  if (typeof reference !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(reference) ||
      isoDate_(Number(reference.slice(0,4)), Number(reference.slice(5,7)), Number(reference.slice(8))) !== reference) {
    fail_('Некорректная дата нового периода.');
  }
  const dates = catalog.dates.slice().sort();
  const first = dates[0];
  // Monthly templates with days 1..28 have an unambiguous boundary even in February.
  if (Number(first.slice(8)) > 28 || addDays_(monthStart_(first), -1) !== dates[dates.length - 1] ||
      dates.some((d, i) => d !== addDays_(first, i))) {
    fail_('Не удалось определить месячный шаблон. Создайте новый лист вручную.');
  }
  if (reference <= dates[dates.length - 1]) fail_('Текущий период ещё не завершён.');
  let start = monthStart_(first);
  for (let i = 0; i < 120 && reference >= monthStart_(start); i++) start = monthStart_(start);
  const end = addDays_(monthStart_(start), -1);
  if (reference > end) fail_('Слишком большой разрыв между периодами. Создайте лист вручную.');
  function short(d) { return d.slice(8) + '.' + d.slice(5, 7); }
  return {start: start, end: end, title: short(start) + ' - ' + short(end)};
}

function createPeriod_(book, sheet, input) {
  validKey_(input.key);
  const fingerprint = hash_(JSON.stringify({action: 'create_period', sheet_id: input.sheet_id,
    revision: input.revision, reference_date: input.reference_date, start: input.start, end: input.end}));
  const journal = journal_(book);
  const repeated = repeat_(journal, input.key, fingerprint);
  if (repeated) return repeated;
  const plan = period_(book, sheet, input);
  const catalog = catalog_(book, sheet);
  if (catalog.revision !== input.revision || plan.start !== input.start || plan.end !== input.end) {
    fail_('Шаблон периода изменился. Запросите создание нового периода ещё раз.');
  }
  const sheets = book.getSheets();
  const ids = new Set(sheets.map(s => s.getSheetId()));
  let newId = Number.parseInt(hash_(input.key).slice(0, 7), 16) + 1;
  while (ids.has(newId)) newId++;
  let title = plan.title;
  if (sheets.some(s => s.getName() === title)) title += ' ' + plan.start.slice(0, 4);
  if (sheets.some(s => s.getName() === title)) fail_('Такой период уже существует. Проверьте порядок вкладок.');
  const requests = [{duplicateSheet: {sourceSheetId: sheet.getSheetId(), newSheetId: newId,
    newSheetName: title, insertSheetIndex: sheets.length}}];
  const serial = Math.round((Date.parse(plan.start + 'T00:00:00Z') - Date.UTC(1899, 11, 30)) / 86400000);
  requests.push(cellUpdate_(newId, 4, 5, {numberValue: serial}));
  const days = [];
  for (let d = plan.start; d <= plan.end; d = addDays_(d, 1)) days.push(d);
  requests.push({updateCells: {
    range: {sheetId: newId, startRowIndex: 12, endRowIndex: 13, startColumnIndex: 8, endColumnIndex: 39},
    rows: [{values: Array.from({length: 31}, (_, i) => i < days.length ?
      {userEnteredValue: {numberValue: Number(days[i].slice(8))}} : {})}], fields: 'userEnteredValue'}});
  Object.values(catalog.rows).forEach(row => {
    requests.push({repeatCell: {range: {sheetId: newId, startRowIndex: row - 1, endRowIndex: row,
      startColumnIndex: 8, endColumnIndex: 39}, cell: {}, fields: 'userEnteredValue'}});
  });
  // Clone, clear expense inputs, set dates and save the receipt atomically.
  const receipt = {sheet_id: newId, title: title, start: plan.start, end: plan.end};
  requests.push(appendReceipt_(journal, input.key, fingerprint, receipt));
  Sheets.Spreadsheets.batchUpdate({requests: requests}, book.getId());
  return receipt;
}
