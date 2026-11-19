import { readdir, readFile } from 'node:fs/promises';
import { join, relative, basename } from 'node:path';
import { fileURLToPath } from 'node:url';

import { isIgnoredDir, isIgnoredFile } from './vault-ignore.mjs';

export async function listNotes(root, dir = root) {
  const out = [];
  const atRoot = dir === root;
  for (const e of await readdir(dir, { withFileTypes: true })) {
    if (e.isDirectory()) {
      if (isIgnoredDir(e.name, atRoot)) continue;
      out.push(...(await listNotes(root, join(dir, e.name))));
    } else if (e.isFile() && e.name.endsWith('.md') && !isIgnoredFile(e.name, atRoot)) {
      out.push(join(dir, e.name));
    }
  }
  return out;
}

// Returns text lines with frontmatter and fenced code removed, and the info strings of the fences opened.
function scan(text) {
  const lines = text.split(/\r?\n/);
  let i = 0;
  if (lines[0] === '---') {
    const end = lines.indexOf('---', 1);
    if (end > 0) i = end + 1;
  }
  const out = [];
  const fences = [];
  let fence = null;
  for (; i < lines.length; i++) {
    const line = lines[i];
    const m = /^[ \t]*(`{3,}|~{3,})/.exec(line); // отступ любой: блоки внутри списков
    if (fence) {
      if (m && m[1][0] === fence.char && m[1].length >= fence.len && /^\s*$/.test(line.slice(m[0].length))) {
        fence = null;
      }
      continue;
    }
    if (m) {
      fence = { char: m[1][0], len: m[1].length };
      fences.push(line.slice(m[0].length).trim());
      continue;
    }
    out.push(line);
  }
  return { lines: out, fences };
}

const proseLines = (text) => scan(text).lines;

const stripInlineCode = (line) => line.replace(/(`+)[^`]*?\1/g, '');

function parse(text) {
  const headings = new Set();
  const links = [];
  for (const line of proseLines(text)) {
    const h = /^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$/.exec(line);
    if (h) headings.add(h[1].trim());
    for (const m of stripInlineCode(line).matchAll(/\[\[([^\]]+)\]\]/g)) links.push(m[1]);
  }
  return { headings, links };
}

// Ожидаемые числа для check-dist: диаграммы mermaid и callout-блоки `> [!type]` вне других код-блоков.
export async function countVault(vaultDir) {
  let mermaid = 0;
  let asides = 0;
  for (const f of await listNotes(vaultDir)) {
    const { lines, fences } = scan(await readFile(f, 'utf8'));
    mermaid += fences.filter((info) => /^mermaid(?:\s|$)/.test(info)).length;
    asides += lines.filter((l) => /^ {0,3}>\s*\[![A-Za-z][\w-]*\]/.test(l)).length;
  }
  return { mermaid, asides };
}

export async function checkVault(vaultDir) {
  const files = await listNotes(vaultDir);
  const byName = new Map();
  const parsed = new Map();
  for (const f of files) {
    const name = basename(f, '.md');
    byName.set(name, [...(byName.get(name) ?? []), f]);
    parsed.set(f, parse(await readFile(f, 'utf8')));
  }

  const problems = [];
  for (const file of files) {
    const rel = relative(vaultDir, file);
    for (const link of parsed.get(file).links) {
      const target = link.split(/\\?\|/)[0].trim();
      const hash = target.indexOf('#');
      const notePart = (hash < 0 ? target : target.slice(0, hash)).trim();
      const heading = hash < 0 ? null : target.slice(hash + 1).trim();
      const report = (reason) => problems.push({ file: rel, link, reason });

      let dest = file;
      if (notePart) {
        const name = basename(notePart.replace(/\.md$/, ''));
        const hits = byName.get(name) ?? [];
        if (hits.length === 0) { report('no such note'); continue; }
        if (hits.length > 1) { report('ambiguous note name'); continue; }
        dest = hits[0];
      }
      if (heading && !heading.startsWith('^') && !parsed.get(dest).headings.has(heading)) {
        report('no such heading');
      }
    }
  }
  return problems;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const dir = process.argv[2];
  if (!dir) {
    console.error('usage: check-vault.mjs <vaultDir>');
    process.exit(2);
  }
  const problems = await checkVault(dir);
  for (const p of problems) console.log(`${p.file}: [[${p.link}]] — ${p.reason}`);
  process.exit(problems.length ? 1 : 0);
}
