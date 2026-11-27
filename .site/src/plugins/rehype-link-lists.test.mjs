import { test } from 'node:test';
import assert from 'node:assert/strict';
import rehypeLinkLists, { toLinkLists } from './rehype-link-lists.mjs';

// Один узел-результат (абзац целиком стал списком) либо null.
const toLinkList = (node) => {
  const out = toLinkLists(node);
  return out && out.length === 1 ? out[0] : out;
};

const t = (value) => ({ type: 'text', value });
const a = (href, label = href) => ({ type: 'element', tagName: 'a', properties: { href }, children: [t(label)] });
const el = (tagName, children, properties = {}) => ({ type: 'element', tagName, properties, children });

// Текст элемента без разметки — чтобы сравнивать пункты списка коротко.
const text = (n) => (n.type === 'text' ? n.value : (n.children ?? []).map(text).join(''));
const items = (ul) => ul.children.map(text);

test('paragraph «Рядом:» becomes label + list, trailing dot dropped', () => {
  const p = el('p', [t('Рядом: '), a('/a', 'A'), t(' · '), a('/b', 'B'), t(' (пояснение) · '), a('/c', 'C'), t('.')]);
  const out = toLinkList(p);
  assert.equal(out.tagName, 'div');
  assert.deepEqual(out.properties.className, ['link-list']);
  const [label, ul] = out.children;
  assert.equal(text(label), 'Рядом:');
  assert.equal(ul.tagName, 'ul');
  assert.deepEqual(items(ul), ['A', 'B (пояснение)', 'C']);
  assert.equal(ul.children[1].children[0].tagName, 'a');
});

test('first item may be plain text (Разделы: asyncio.timeout · ...)', () => {
  const p = el('p', [t('Разделы: asyncio.timeout · '), a('#x', 'X'), t(' · '), a('#y', 'Y')]);
  const [, ul] = toLinkList(p).children;
  assert.deepEqual(items(ul), ['asyncio.timeout', 'X', 'Y']);
});

test('nested parenthesised link stays inside its item', () => {
  const p = el('p', [t('Разделы: '), a('#a', 'A'), t(' ('), a('#a1', 'A1'), t(') · '), a('#b', 'B'), t(' · '), a('#c', 'C')]);
  const [, ul] = toLinkList(p).children;
  assert.deepEqual(items(ul), ['A (A1)', 'B', 'C']);
});

test('list item with bold label gets a nested list', () => {
  const li = el('li', [el('strong', [t('Язык:')]), t(' '), a('/a', 'A'), t(' · '), a('/b', 'B'), t(' · '), a('/c', 'C')]);
  const out = toLinkList(li);
  assert.equal(out.tagName, 'li');
  const [label, ul] = out.children;
  assert.equal(label.tagName, 'strong');
  assert.deepEqual(ul.properties.className, ['link-list']);
  assert.deepEqual(items(ul), ['A', 'B', 'C']);
});

test('list item with nested list: only the first line is rewritten, nothing duplicated', () => {
  const nested = el('ul', [el('li', [t('вложенный')])]);
  const li = el('li', [el('strong', [t('Язык:')]), t(' '), a('/a', 'A'), t(' · '), a('/b', 'B'), t(' · '), a('/c', 'C'), t('.\n'), nested]);
  const [out] = toLinkLists(li);
  const [label, ul, ...rest] = out.children;
  assert.equal(label.tagName, 'strong');
  assert.deepEqual(items(ul), ['A', 'B', 'C']);
  assert.deepEqual(rest.map(text), ['\n', 'вложенный']);
});

test('parenthesised notes, URLs and own dots are not misread', () => {
  // проза между двумя скобками — не пояснение к ссылке
  assert.equal(toLinkList(el('p', [t('Рядом: '), a('/a', 'A'), t(' (x) и ещё что-то (y), '), a('/b', 'B')])), null);
  // URL в начале — не метка
  assert.equal(toLinkList(el('p', [t('https://x.com · '), a('/a'), t(' · '), a('/b'), t(' · '), a('/c')])), null);
  // точка, которая принадлежит тексту пункта, остаётся
  const p = el('p', [t('Разделы: '), a('#a', 'A'), t(' · '), a('#b', 'B'), t(' · и т.д.')]);
  assert.deepEqual(items(toLinkList(p).children[1]), ['A', 'B', 'и т.д.']);
});

test('leaves lines that are not link lists', () => {
  const cases = [
    // незнакомая метка и два пункта — не список
    el('p', [t('Итог: '), a('/a'), t(' · '), a('/b')]),
    // запятые, но после ссылок идёт фраза — это проза, а не список
    el('p', [t('Рядом: '), a('/a', 'A'), t(', '), a('/b', 'B'), t('. Как это выглядит в Docker: '), a('/c', 'C'), t(' (слои).')]),
    // одна ссылка — не список
    el('p', [t('Рядом: '), a('/a'), t('.')]),
    // нет метки
    el('li', [a('https://x', 'Task'), t(' · Medium · нюанс: '), a('#y')]),
    // метка, но пункты без ссылок
    el('li', [el('strong', [t('Сложность:')]), t(' T: O(log n · C), где C — цена · S: O(1) · ещё')]),
    // жирный заголовок абзаца не заканчивается двоеточием
    el('p', [el('strong', [t('asyncio: ошибки.')]), t(' текст · '), a('/a'), t(' · '), a('/b')]),
    // обычный абзац
    el('p', [t('Просто текст со ссылкой '), a('/a'), t('.')]),
  ];
  for (const node of cases) assert.equal(toLinkList(node), null);
});

test('known label: two items are enough, items may be plain text', () => {
  const two = el('p', [t('Рядом: '), a('/a', 'A'), t(' (пояснение) · '), a('/b', 'B'), t('.')]);
  assert.deepEqual(items(toLinkList(two).children[1]), ['A (пояснение)', 'B']);
  const plain = el('p', [t('Разделы: Semaphore: сколько летит · Забытый await · '), a('#q', 'Что спросят')]);
  assert.deepEqual(items(toLinkList(plain).children[1]), ['Semaphore: сколько летит', 'Забытый await', 'Что спросят']);
});

test('known label with commas: split outside parentheses', () => {
  const p = el('p', [
    t('Рядом: '), a('/l', 'Linux'), t(' (процессы, память, контейнеры), '), a('/d', 'Docker'), t(' (образы, выкатка), '), a('/s', 'SQL'), t('.'),
  ]);
  assert.deepEqual(items(toLinkList(p).children[1]), ['Linux (процессы, память, контейнеры)', 'Docker (образы, выкатка)', 'SQL']);
});

test('lines of a paragraph: intro stays a paragraph, «Рядом» and «Разделы» become lists', () => {
  const p = el('p', [
    t('Вступление со '), el('code', [t('wraps')]), t('.\nРядом: '), a('/a', 'A'), t(' · '), a('/b', 'B'), t(' · '), a('/c', 'C'),
    t('.\nРазделы: '), a('#x', 'X'), t(' · '), a('#y', 'Y'), t(' · '), a('#z', 'Z'),
  ]);
  const out = toLinkLists(p);
  assert.equal(out.length, 3);
  assert.equal(out[0].tagName, 'p');
  assert.equal(text(out[0]), 'Вступление со wraps.');
  assert.deepEqual(items(out[1].children[1]), ['A', 'B', 'C']);
  assert.deepEqual(items(out[2].children[1]), ['X', 'Y', 'Z']);
});

test('text lines around a list are regrouped with their line breaks', () => {
  const p = el('p', [t('раз\nдва\nРядом: '), a('/a'), t(' · '), a('/b'), t(' · '), a('/c'), t('\nтри')]);
  const out = toLinkLists(p);
  assert.deepEqual(out.map((n) => n.tagName), ['p', 'div', 'p']);
  assert.equal(text(out[0]), 'раз\nдва');
  assert.equal(text(out[2]), 'три');
});

test('plugin replaces paragraph and rewrites list item children', () => {
  const plugin = rehypeLinkLists();
  assert.deepEqual(plugin.element.filter, ['p', 'li']);
  const calls = [];
  const ctx = {
    replaceNode: (n, v) => calls.push(['replace', n.tagName, v.length]),
    setField: (n, key, v) => calls.push(['set', n.tagName, key, v.length]),
  };
  plugin.element.visit(el('p', [t('Рядом: '), a('/a'), t(' · '), a('/b'), t(' · '), a('/c')]), ctx);
  plugin.element.visit(el('li', [el('strong', [t('X:')]), t(' '), a('/a'), t(' · '), a('/b'), t(' · '), a('/c')]), ctx);
  plugin.element.visit(el('p', [t('обычный')]), ctx);
  assert.deepEqual(calls, [['replace', 'p', 1], ['set', 'li', 'children', 2]]);
});
