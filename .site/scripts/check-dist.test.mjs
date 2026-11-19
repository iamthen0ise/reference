import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cp, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { checkDist } from './check-dist.mjs';

const FIXTURE = fileURLToPath(new URL('./fixtures/dist/', import.meta.url));
const OPTS = { base: '/reference', mermaid: 1, asides: 1 };

async function copy() {
  const dir = await mkdtemp(join(tmpdir(), 'check-dist-'));
  await cp(FIXTURE, dir, { recursive: true });
  return dir;
}

async function mutate(file, fn) {
  const dir = await copy();
  const p = join(dir, file);
  await writeFile(p, fn(await readFile(p, 'utf8')));
  return dir;
}

test('clean dist passes', async () => {
  assert.deepEqual(await checkDist(FIXTURE, OPTS), []);
});

test('broken page link reported', async () => {
  const dir = await mutate('index.html', (h) => h.replace('</body>', '<a href="/reference/nope/">x</a></body>'));
  const p = await checkDist(dir, OPTS);
  assert.equal(p.length, 1);
  assert.match(p[0], /nope/);
});

test('broken relative link reported', async () => {
  const dir = await mutate('a/index.html', (h) => h.replace('</body>', '<a href="../zzz/">x</a></body>'));
  assert.match((await checkDist(dir, OPTS)).join('\n'), /zzz/);
});

test('missing anchor reported', async () => {
  const dir = await mutate('index.html', (h) => h.replace('</body>', '<a href="b/#missing">x</a></body>'));
  const p = await checkDist(dir, OPTS);
  assert.equal(p.length, 1);
  assert.match(p[0], /missing/);
});

test('anchor: cyrillic percent-encoded href resolves', async () => {
  const dir = await mutate('index.html', (h) =>
    h.replace('</body>', '<a href="/reference/a/#%D0%B8%D0%BD%D0%B4%D0%B5%D0%BA%D1%81%D1%8B">x</a></body>'));
  assert.deepEqual(await checkDist(dir, OPTS), []);
});

test('raw wikilink and callout in text reported', async () => {
  const dir = await mutate('b/index.html', (h) => h.replace('</body>', '<p>see [[Note]]</p><p>[!tip] hi</p></body>'));
  const p = (await checkDist(dir, OPTS)).join('\n');
  assert.match(p, /\[\[/);
  assert.match(p, /\[!/);
});

test('ignores code', async () => {
  // fixture a/index.html already holds [[1, 2]] and [!tip] in pre/code, and [[..]] in an attribute
  assert.deepEqual(await checkDist(FIXTURE, OPTS), []);
  const dir = await mutate('b/index.html', (h) =>
    h.replace('</body>', '<script>var a=[[1]];</script><style>/*[!x]*/</style></body>'));
  assert.deepEqual(await checkDist(dir, OPTS), []);
});

test('ignores code in Expressive Code copy button (raw ">" inside data-code)', async () => {
  const copy = '<div class="copy"><div aria-live="polite"></div><button title="Копировать" data-copied="Скопировано!" ' +
    'data-code="self.buckets = [[] for _ in range(2)]\u007fdef f() -> int: return a > b">' +
    '<div></div></button></div>';
  const dir = await mutate('b/index.html', (h) => h.replace('</body>', copy + '</body>'));
  assert.deepEqual(await checkDist(dir, OPTS), []);
});

test('raw wikilink in prose next to a quoted attribute with ">" still reported', async () => {
  const dir = await mutate('b/index.html', (h) =>
    h.replace('</body>', '<a title="a > b" href="/reference/">x</a><p>[[Note]]</p></body>'));
  assert.match((await checkDist(dir, OPTS)).join('\n'), /\[\[/);
});

test('mermaid in code highlight reported', async () => {
  const dir = await mutate('b/index.html', (h) =>
    h.replace('</body>', '<pre data-language="mermaid"><code class="language-mermaid">x</code></pre></body>'));
  assert.match((await checkDist(dir, OPTS)).join('\n'), /mermaid/);
});

test('only rendered mermaid pictures are counted', async () => {
  const dir = await mutate('b/index.html', (h) =>
    h.replace('</body>', '<picture><source srcset="x.png"><img src="x.png" alt=""></picture></body>'));
  assert.deepEqual(await checkDist(dir, OPTS), []);
});

test('unrendered mermaid source (pre.mermaid) reported', async () => {
  const dir = await mutate('b/index.html', (h) => h.replace('</body>', '<pre class="mermaid">graph TD</pre></body>'));
  assert.match((await checkDist(dir, OPTS)).join('\n'), /unrendered mermaid/);
});

test('mermaid and aside counts enforced', async () => {
  const p = await checkDist(FIXTURE, { base: '/reference', mermaid: 32, asides: 30 });
  assert.equal(p.length, 2);
  assert.ok(p.some((x) => /mermaid/.test(x) && /32/.test(x) && /1/.test(x)));
  assert.ok(p.some((x) => /aside/.test(x) && /30/.test(x) && /1/.test(x)));
});
