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
    getRange(row, col) {
      if (row === 13) return {getValues: () => [days]};
      if (row === 14 && col === 2) return {getValues: () => categories};
      const key = row + ':' + col;
      const item = table.get(key) || {value: ''};
      return {getFormula: () => item.formula || '', getValue: () => item.value};
    }
  };
  const journal = {
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
    Sheets: {Spreadsheets: {batchUpdate(body, id) {
      assert.equal(id, 'book');
      if (state.fail) throw new Error('timeout');
      state.batches.push(body);
      // Model the atomic boundary: nothing is applied until validation succeeded.
      for (const request of body.requests) {
        if (request.appendCells) {
          receipts.push(request.appendCells.rows[0].values.map(v => v.userEnteredValue.stringValue));
        } else {
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
