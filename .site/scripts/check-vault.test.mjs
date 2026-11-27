import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { checkVault, countVault } from './check-vault.mjs';

const here = dirname(fileURLToPath(import.meta.url));

async function tmpVault(files) {
  const dir = await mkdtemp(join(tmpdir(), 'vault-'));
  for (const [rel, body] of Object.entries(files)) {
    await mkdir(dirname(join(dir, rel)), { recursive: true });
    await writeFile(join(dir, rel), body);
  }
  return dir;
}

test('ok vault has no problems', async () => {
  assert.deepEqual(await checkVault(join(here, 'fixtures/vault-ok')), []);
});

test('reports missing note, missing heading, ambiguous name', async () => {
  const problems = await checkVault(join(here, 'fixtures/vault-broken'));
  assert.deepEqual(
    problems.map((p) => p.reason).sort(),
    ['ambiguous note name', 'no such heading', 'no such note'],
  );
});

test('ignores code', async () => {
  const dir = await tmpVault({
    'A.md': '```python\n[[Нет]]\n```\n\n~~~\n[[Нет]]\n~~~\n\nтекст `[[Нет]]` ещё\n',
  });
  try {
    assert.deepEqual(await checkVault(dir), []);
  } finally {
    await rm(dir, { recursive: true });
  }
});

test('ignores frontmatter', async () => {
  const dir = await tmpVault({ 'A.md': '---\nx: "[[Нет]]"\n---\ntext\n' });
  try {
    assert.deepEqual(await checkVault(dir), []);
  } finally {
    await rm(dir, { recursive: true });
  }
});

test('heading inside fence is not a heading', async () => {
  const dir = await tmpVault({
    'A.md': '[[B#Фейк]]\n',
    'B.md': '```\n# Фейк\n```\n',
  });
  try {
    const problems = await checkVault(dir);
    assert.equal(problems.length, 1);
    assert.equal(problems[0].reason, 'no such heading');
  } finally {
    await rm(dir, { recursive: true });
  }
});

test('skips ignored dirs', async () => {
  const dir = await tmpVault({
    'A.md': '[[D]]\n',
    'docs/D.md': '# d\n',
    '.site/D.md': '# d\n',
    'node_modules/D.md': '# d\n',
  });
  try {
    const problems = await checkVault(dir);
    assert.equal(problems.length, 1);
    assert.equal(problems[0].reason, 'no such note');
  } finally {
    await rm(dir, { recursive: true });
  }
});

test('escaped pipe \\| in a table cell is the alias separator', async () => {
  const dir = await tmpVault({
    'A.md': '| a | b |\n|---|---|\n| x | [[B#Раздел\\|алиас]] и [[B\\|ещё]] |\n',
    'B.md': '## Раздел\n',
  });
  try {
    assert.deepEqual(await checkVault(dir), []);
  } finally {
    await rm(dir, { recursive: true });
  }
});

test('only root docs/ and README.md are ignored: nested ones are checked', async () => {
  const dir = await tmpVault({
    'README.md': '[[Нет1]]\n',
    'docs/X.md': '[[Нет2]]\n',
    'Python/README.md': '[[Нет3]]\n',
    'Python/docs/Y.md': '[[Нет4]]\n',
    'a/node_modules/Z.md': '[[Нет5]]\n',
    'a/.hidden/W.md': '[[Нет6]]\n',
  });
  try {
    const problems = await checkVault(dir);
    assert.deepEqual(problems.map((p) => p.file).sort(), ['Python/README.md', 'Python/docs/Y.md']);
  } finally {
    await rm(dir, { recursive: true });
  }
});

test('Labs/: lab pages are checked, lab code dirs inside are ignored', async () => {
  const dir = await tmpVault({
    'Labs/Labs.md': '[[Нет1]]\n',
    'Labs/some-lab/README.md': '[[Нет2]]\n',
    'Labs/some-lab/docs/X.md': '[[Нет3]]\n',
    'Python/some-lab/README.md': '[[Нет4]]\n',
  });
  try {
    const problems = await checkVault(dir);
    assert.deepEqual(problems.map((p) => p.file).sort(), ['Labs/Labs.md', 'Python/some-lab/README.md']);
  } finally {
    await rm(dir, { recursive: true });
  }
});

test('countVault counts mermaid fences and callouts outside other fences', async () => {
  const dir = await tmpVault({
    'A.md': [
      '---', 'x: 1', '---',
      '```mermaid', 'graph TD', '> [!note] в диаграмме', '```',
      '> [!tip] раз', '> текст', '',
      '> [!warning]- свёрнуто',
      '````md', '```mermaid', '> [!note] в примере', '```', '````',
      '~~~', '> [!note] в тильдах', '~~~',
      '',
    ].join('\n'),
    'sub/B.md': '```mermaid\ngraph LR\n```\n\n> [!info] два\n',
    'sub/L.md': '- пункт\n\t```mermaid\n\tgraph LR\n\t```\n\t```py\n\t[[Нет]]\n\t```\n',
    'README.md': '```mermaid\nx\n```\n> [!note] игнор\n',
    'docs/C.md': '> [!note] игнор\n',
  });
  try {
    assert.deepEqual(await countVault(dir), { mermaid: 3, asides: 3 });
  } finally {
    await rm(dir, { recursive: true });
  }
});
