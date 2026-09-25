const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const test = require('node:test');
const assert = require('node:assert/strict');

function harness({start = '2026-08-10', header, rows, cells = {}} = {}) {
  const table = new Map();
  const receipts = [];
  for (const [key, value] of Object.entries(cells)) table.set(key, {...value});
  const days = header || [...Array.from({length:22}, (_, i) => i+10), ...Array.from({length:9}, (_, i) => i+1)];
  const categories = rows || [[1, 'Продукты', '', 'Супермаркеты'], [2, 'Рестораны', '', 'Доставки'], ['Итого за день', '', '', '']];
  const sheet = {
    getSheetId: () => 10, getName: () => '10.08 - 09.09', isSheetHidden: () => false,
    getLastRow: () => 13 + categories.length,
    getRange(row, col, height, width) {
      if (row === 13) return {getValues: () => [days]};
      if (row === 14 && col === 2) return {getValues: () => categories};
      if (col === 9 && width === 31) return {getValues: () => [Array.from({length: 31}, (_, i) => (table.get(row + ':' + (9+i)) || {value: ''}).value)]};
      const key = row + ':' + col;
      const item = table.get(key) || {value: ''};
      return {getFormula: () => item.formula || '', getValue: () => item.value};
    }
  };
  const journal = {
    getParent: () => book,
    getSheetId: () => 99,
    getRange(row) {
      if (row === 'A:A') return {createTextFinder: key => ({matchEntireCell() {return this;},
        findNext() {const idx = receipts.findIndex(r => r[0] === key); return idx < 0 ? null : {getRow: () => idx+2};}})};
      return {getValues: () => [[receipts[row-2][1], receipts[row-2][2]]]};
    }
  };
  const book = {getId: () => 'book', getSheetByName: () => journal, getSheets: () => [sheet],
    getSpreadsheetTimeZone: () => 'Asia/Novosibirsk'};
  const state = {batches: [], fail: false, table, receipts};
  const context = vm.createContext({
    console, Date,
    PropertiesService: {getScriptProperties: () => ({getProperty: name => name === 'SHEET_START_DATES' ? JSON.stringify({'10': start}) : null})},
    Utilities: {DigestAlgorithm: {SHA_256: 'sha256'}, Charset: {UTF_8: 'UTF-8'},
      computeDigest: (_, text, charset) => {
        assert.equal(charset, 'UTF-8');
        return Array.from(crypto.createHash('sha256').update(text, 'utf8').digest());
      }},
    Sheets: {Spreadsheets: {Values: {get(id, range) {
      const row = Number(range.match(/!B(\d+):C/)[1]);
      return {values: [[receipts[row-2][1], receipts[row-2][2]]]};
    }}, batchUpdate(body, id) {
      assert.equal(id, 'book');
      if (state.fail) throw new Error('timeout');
      state.batches.push(body);
      // Model the atomic boundary: nothing is applied until validation succeeded.
      for (const request of body.requests) {
        if (request.appendCells) {
          receipts.push(request.appendCells.rows[0].values.map(v => v.userEnteredValue.stringValue));
        } else if (request.updateCells && request.updateCells.range.sheetId === 99) {
          const r = request.updateCells;
          receipts[r.range.startRowIndex - 1][2] = r.rows[0].values[0].userEnteredValue.stringValue;
        } else if (request.updateCells && request.updateCells.range.sheetId === 10) {
          const r = request.updateCells;
          const key = (r.range.startRowIndex + 1) + ':' + (r.range.startColumnIndex + 1);
          const value = r.rows[0].values[0].userEnteredValue;
          table.set(key, {value: value.numberValue ?? 123, formula: value.formulaValue});
          assert.equal(r.fields, 'userEnteredValue');
        }
      }
    }}}
  });
  vm.runInContext(fs.readFileSync('integrations/google_sheets/Code.gs', 'utf8'), context);
  const catalog = context.catalog_(book, sheet);
  function input(overrides = {}) {
    return {key: 'telegram:123:100:1', sheet_id: 10, revision: catalog.revision,
      expenses: [{category_id: catalog.categories[0].id, date: catalog.dates[0], amount_minor: 25050, description: 'кофе'}], ...overrides};
  }
  return {context, book, sheet, catalog, input, state};
}

test('derives full category paths and date rollover from selected sheet', () => {
  const h = harness();
  assert.equal(h.catalog.categories[0].label, 'Продукты / Супермаркеты');
  assert.equal(h.catalog.dates[0], '2026-08-10');
  assert.equal(h.catalog.dates.at(-1), '2026-09-09');
  assert.equal(h.catalog.categories.length, 2); // no total row
});

test('month/year rollover and impossible days are handled', () => {
  const december = harness({start: '2026-12-10'});
  assert.equal(december.catalog.dates.at(-1), '2027-01-09');
  const september = harness({start: '2026-09-10'});
  assert(!september.catalog.dates.includes('2026-09-31'));
  assert(september.catalog.dates.includes('2026-10-01'));
});

test('equal-length Cyrillic category names have distinct UTF-8 identifiers', () => {
  const h = harness({rows: [[1,'Родители','','Пенсия'], ['','','','Другое'], ['Итого','','','']]});
  assert.notEqual(h.catalog.categories[0].id, h.catalog.categories[1].id);
  for (const category of h.catalog.categories) {
    assert.equal(category.id, crypto.createHash('sha256').update(category.label, 'utf8').digest('hex').slice(0, 24));
  }
});

test('duplicate category labels, invalid headers and missing totals fail closed', () => {
  assert.throws(() => harness({rows: [[1,'Еда','',''], [2,'Еда','',''], ['Итого','','','']]}), /повторяются/);
  assert.throws(() => harness({header: [10, 11, 11]}), /повторяется/);
  assert.throws(() => harness({rows: [[1,'Еда','','']]}), /Итого/);
});

test('empty and numeric cells add exact decimal amount; unrelated cells unchanged', () => {
  const h = harness({cells: {'14:9': {value: 100.25}, '15:9': {value: 77}}});
  h.context.write_(h.book, h.sheet, h.input());
  assert.equal(h.state.table.get('14:9').value, 350.75);
  assert.equal(h.state.table.get('15:9').value, 77);
  assert.equal(h.state.receipts.length, 1);
  assert.equal(h.state.batches[0].requests.length, 2);
});

test('existing formulas are preserved as part of the new expression', () => {
  const h = harness({cells: {'14:9': {value: 589.5, formula: '=(438+741)/2'}}});
  h.context.write_(h.book, h.sheet, h.input());
  assert.equal(h.state.table.get('14:9').formula, '=((438+741)/2)+(25050/100)');
});

test('repeat delivery returns same receipt and does not increase cell twice', () => {
  const h = harness();
  const first = h.context.write_(h.book, h.sheet, h.input());
  const second = h.context.write_(h.book, h.sheet, h.input());
  assert.equal(first.recorded_at, second.recorded_at);
  assert.equal(h.state.table.get('14:9').value, 250.5);
  assert.equal(h.state.batches.length, 1);
});

test('same message with different amount is rejected', () => {
  const h = harness();
  h.context.write_(h.book, h.sheet, h.input());
  const changed = h.input(); changed.expenses[0].amount_minor = 999;
  assert.throws(() => h.context.write_(h.book, h.sheet, changed), /уже записано/);
});

test('multiple expenses in one cell accumulate and commit with one receipt', () => {
  const h = harness();
  const body = h.input(); body.expenses.push({...body.expenses[0], amount_minor: 100});
  h.context.write_(h.book, h.sheet, body);
  assert.equal(h.state.table.get('14:9').value, 251.5);
  assert.equal(h.state.batches.length, 1);
});

test('changed structure, invented category, dates and invalid amounts never write', () => {
  for (const change of [b => b.revision='old', b => b.expenses[0].category_id='invented',
      b => b.expenses[0].date='2026-10-01', b => b.expenses[0].amount_minor=-5,
      b => b.expenses[0].amount_minor=1.5]) {
    const h = harness(); const body = h.input(); change(body);
    assert.throws(() => h.context.write_(h.book, h.sheet, body));
    assert.equal(h.state.batches.length, 0);
  }
});

test('bad destination in a batch prevents all writes', () => {
  const h = harness({cells: {'15:9': {value: '#REF!'}}});
  const body = h.input(); body.expenses.push({...body.expenses[0], category_id: h.catalog.categories[1].id});
  assert.throws(() => h.context.write_(h.book, h.sheet, body), /ошибка/);
  assert.equal(h.state.batches.length, 0);
  assert.equal(h.state.receipts.length, 0);
});

test('failed atomic batch has no receipt; retry applies once', () => {
  const h = harness(); h.state.fail = true;
  assert.throws(() => h.context.write_(h.book, h.sheet, h.input()));
  assert.equal(h.state.receipts.length, 0);
  h.state.fail = false;
  h.context.write_(h.book, h.sheet, h.input());
  h.context.write_(h.book, h.sheet, h.input());
  assert.equal(h.state.batches.length, 1);
});


test('new last sheet prevents a fresh write to a previous period', () => {
  const h = harness();
  h.book.getSheets = () => [h.sheet, {getSheetId: () => 20, getName: () => 'New', isSheetHidden: () => false}];
  assert.throws(() => h.context.write_(h.book, h.sheet, h.input()), /Последний лист изменился/);
  assert.equal(h.state.batches.length, 0);
});

test('hidden service sheets do not change the expense destination', () => {
  const h = harness();
  h.book.getSheets = () => [h.sheet, {isSheetHidden: () => true}];
  h.context.write_(h.book, h.sheet, h.input());
  assert.equal(h.state.batches.length, 1);
});

test('committed retry returns its receipt even after a new period appears', () => {
  const h = harness();
  const first = h.context.write_(h.book, h.sheet, h.input());
  h.book.getSheets = () => [h.sheet, {getSheetId: () => 20, getName: () => 'New', isSheetHidden: () => false}];
  const retry = h.context.write_(h.book, h.sheet, h.input());
  assert.equal(first.recorded_at, retry.recorded_at);
  assert.equal(h.state.batches.length, 1);
});

function amendment(h, expenses = [], version = 0, key = 'telegram:123:100:op:2') {
  return {key, target_key: h.input().key, sheet_id: 10, revision: h.catalog.revision, version, expenses};
}

test('undo subtracts only this expense, preserves other purchases and is idempotent', () => {
  const h = harness({cells: {'14:9': {value: 100}}});
  h.context.write_(h.book, h.sheet, h.input());
  const body = amendment(h);
  h.context.amend_(h.book, h.sheet, body);
  assert.equal(h.state.table.get('14:9').value, 100);
  assert.equal(JSON.parse(h.state.receipts[0][2]).version, 1);
  h.context.amend_(h.book, h.sheet, body);
  assert.equal(h.state.batches.length, 2);
  assert.equal(h.state.table.get('14:9').value, 100);
});

test('edit moves amount to new category and date atomically', () => {
  const h = harness({cells: {'14:9': {value: 10}, '15:10': {value: 50}}});
  h.context.write_(h.book, h.sheet, h.input());
  const expense = {...h.input().expenses[0], category_id: h.catalog.categories[1].id,
    date: h.catalog.dates[1], amount_minor: 35000};
  const body = amendment(h, [expense]);
  h.context.amend_(h.book, h.sheet, body);
  assert.equal(h.state.table.get('14:9').value, 10);
  assert.equal(h.state.table.get('15:10').value, 400);
  assert.equal(h.state.batches[1].requests.length, 4); // two cells, record revision, operation receipt
  assert.equal(JSON.parse(h.state.receipts[0][2]).expenses[0].category_id, expense.category_id);
  h.context.amend_(h.book, h.sheet, amendment(h, [], 1, 'telegram:123:100:op:3'));
  assert.equal(h.state.table.get('15:10').value, 50);
});

test('stale version, foreign owner, old structure and removed manual amounts block amendments', () => {
  for (const mutate of [b => b.version=4, b => b.key='telegram:123:200:op:2',
      b => b.revision='old', (b,h) => h.state.table.set('14:9', {value: 1})]) {
    const h = harness();
    h.context.write_(h.book, h.sheet, h.input());
    const body = amendment(h); mutate(body, h);
    assert.throws(() => h.context.amend_(h.book, h.sheet, body));
    assert.equal(h.state.batches.length, 1);
    assert.equal(JSON.parse(h.state.receipts[0][2]).version, 0);
  }
});

test('failed amendment keeps original journal and cells; exact retry succeeds once', () => {
  const h = harness(); h.context.write_(h.book, h.sheet, h.input());
  h.state.fail = true;
  assert.throws(() => h.context.amend_(h.book, h.sheet, amendment(h)));
  assert.equal(h.state.table.get('14:9').value, 250.5);
  assert.equal(JSON.parse(h.state.receipts[0][2]).version, 0);
  h.state.fail = false;
  h.context.amend_(h.book, h.sheet, amendment(h));
  h.context.amend_(h.book, h.sheet, amendment(h));
  assert.equal(h.state.table.get('14:9').value, 0);
  assert.equal(h.state.batches.length, 2);
});

test('undo retains existing formula and applies a signed delta', () => {
  const h = harness({cells: {'14:9': {value: 500, formula: '=500'}}});
  h.context.write_(h.book, h.sheet, h.input());
  h.state.table.get('14:9').value = 750.5; // Sheets evaluates the formula
  h.context.amend_(h.book, h.sheet, amendment(h));
  assert.match(h.state.table.get('14:9').formula, /\+\(-25050\/100\)$/);
  assert.match(h.state.table.get('14:9').formula, /500/);
});

test('summary reads manual values and evaluated formulas exactly once per date and category', () => {
  const h = harness({cells: {'14:9': {value: 100.25}, '14:10': {value: 50},
    '15:9': {value: 200, formula: '=100+100'}}});
  const body = {revision: h.catalog.revision, dates: [h.catalog.dates[0], h.catalog.dates[0]], category_ids: []};
  const all = h.context.summary_(h.book, h.sheet, body);
  assert.equal(all.total_minor, 30025);
  const food = h.context.summary_(h.book, h.sheet, {...body, category_ids: [h.catalog.categories[0].id]});
  assert.equal(food.total_minor, 10025);
  assert.equal(h.state.batches.length, 0);
  assert.throws(() => h.context.summary_(h.book, h.sheet, {...body, dates: ['2025-01-01']}));
  assert.throws(() => h.context.summary_(h.book, h.sheet, {...body, category_ids: ['invented']}));
});

test('summary refuses spreadsheet errors instead of displaying misleading totals', () => {
  const h = harness({cells: {'14:9': {value: '#REF!'}}});
  assert.throws(() => h.context.summary_(h.book, h.sheet, {revision: h.catalog.revision,
    dates: [h.catalog.dates[0]], category_ids: []}), /ошибка/);
});

test('new period follows monthly boundary including February, year rollover and gaps', () => {
  for (const [start, reference, expectedStart, expectedEnd] of [
    ['2026-08-10','2026-09-25','2026-09-10','2026-10-09'],
    ['2026-12-10','2027-01-25','2027-01-10','2027-02-09'],
    ['2028-01-10','2028-02-29','2028-02-10','2028-03-09'],
    ['2026-08-10','2027-04-15','2027-04-10','2027-05-09']]) {
    const h = harness({start});
    const plan = h.context.period_(h.book, h.sheet, {reference_date: reference});
    assert.equal(plan.start, expectedStart); assert.equal(plan.end, expectedEnd);
    assert.equal(h.state.batches.length, 0);
  }
  const h = harness();
  assert.throws(() => h.context.period_(h.book, h.sheet, {reference_date: '2026-08-25'}), /не завершён/);
});

test('create period clones and clears only new expense cells in same batch as receipt', () => {
  const h = harness();
  const plan = h.context.period_(h.book, h.sheet, {reference_date: '2026-09-25'});
  const body = {key: 'telegram:123:100:op:88', sheet_id: 10, revision: h.catalog.revision,
    reference_date: '2026-09-25', start: plan.start, end: plan.end};
  const created = h.context.createPeriod_(h.book, h.sheet, body);
  const requests = h.state.batches[0].requests;
  assert.equal(requests[0].duplicateSheet.newSheetName, '10.09 - 09.10');
  assert.notEqual(created.sheet_id, 10);
  assert(requests.filter(r => r.repeatCell).every(r => r.repeatCell.range.sheetId === created.sheet_id));
  assert.equal(requests.filter(r => r.repeatCell).length, 2);
  const header = requests.find(r => r.updateCells?.range.startRowIndex === 12).updateCells.rows[0].values;
  assert.equal(header.length, 31);
  assert.equal(header[0].userEnteredValue.numberValue, 10);
  assert.equal(header[29].userEnteredValue.numberValue, 9);
  assert.equal(header[30].userEnteredValue, undefined);
  const retry = h.context.createPeriod_(h.book, h.sheet, body);
  assert.equal(retry.sheet_id, created.sheet_id);
  assert.equal(h.state.batches.length, 1);
});

test('changed template or failed batch does not create a partial period', () => {
  const h = harness();
  const plan = h.context.period_(h.book, h.sheet, {reference_date: '2026-09-25'});
  const body = {key:'telegram:123:100:op:88', sheet_id:10, revision:'changed',
    reference_date:'2026-09-25', start:plan.start, end:plan.end};
  assert.throws(() => h.context.createPeriod_(h.book, h.sheet, body), /изменился/);
  body.revision = h.catalog.revision;
  h.state.fail = true;
  assert.throws(() => h.context.createPeriod_(h.book, h.sheet, body));
  assert.equal(h.state.receipts.length, 0);
  assert.equal(h.state.batches.length, 0);
});
