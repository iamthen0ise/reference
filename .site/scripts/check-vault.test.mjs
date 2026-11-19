import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { checkVault } from './check-vault.mjs';

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
