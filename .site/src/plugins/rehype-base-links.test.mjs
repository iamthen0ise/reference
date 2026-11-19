import { test } from 'node:test';
import assert from 'node:assert/strict';
import rehypeBaseLinks from './rehype-base-links.mjs';

// Sätteri hast-плагин: visitor `element.visit(node, ctx)` меняет свойства через ctx.setProperty.
const run = (href, base = '/reference') => {
  const node = { type: 'element', tagName: 'a', properties: { href }, children: [] };
  const ctx = { setProperty: (n, key, value) => { n.properties[key] = value; } };
  const plugin = rehypeBaseLinks({ base });
  assert.deepEqual(plugin.element.filter, ['a']);
  plugin.element.visit(node, ctx);
  return node.properties.href;
};

test('prefixes root-relative links, keeps anchor', () => {
  assert.equal(run('/notes/x/#якорь'), '/reference/notes/x/#якорь');
  assert.equal(run('/notes/x'), '/reference/notes/x');
});

test('leaves already-prefixed and exact base', () => {
  assert.equal(run('/reference/notes/x'), '/reference/notes/x');
  assert.equal(run('/reference'), '/reference');
});

test('does not treat /referencefoo as prefixed', () => {
  assert.equal(run('/referencefoo'), '/reference/referencefoo');
});

test('leaves protocol-relative, absolute, anchor and relative hrefs', () => {
  for (const h of ['//host/x', 'https://a.b/c', '#a', 'x/y', '../z', 'mailto:a@b.c']) {
    assert.equal(run(h), h);
  }
});

test('base with trailing slash and empty base', () => {
  assert.equal(run('/n', '/reference/'), '/reference/n');
  assert.equal(run('/n', ''), '/n');
});

test('a without href is ignored', () => {
  const node = { type: 'element', tagName: 'a', properties: {}, children: [] };
  rehypeBaseLinks({ base: '/r' }).element.visit(node, { setProperty() { throw new Error('no'); } });
});
