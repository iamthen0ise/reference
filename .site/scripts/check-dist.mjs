import { readdir, readFile, stat } from 'node:fs/promises';
import { join, relative, sep, posix } from 'node:path';
import { fileURLToPath } from 'node:url';

const SKIP_DIRS = new Set(['_astro', 'pagefind']);
const SKIP_HREF = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i; // http(s):, mailto:, tel:, data:, //host

async function listPages(root, dir = root) {
  const out = [];
  for (const e of await readdir(dir, { withFileTypes: true })) {
    if (e.isDirectory()) {
      if (dir === root && SKIP_DIRS.has(e.name)) continue;
      out.push(...(await listPages(root, join(dir, e.name))));
    } else if (e.isFile() && e.name.endsWith('.html')) {
      out.push(join(dir, e.name));
    }
  }
  return out;
}

function decode(s) {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

function decodeEntities(s) {
  return s
    .replace(/&quot;/g, '"').replace(/&#39;|&apos;/g, "'")
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}

// Visible text: without comments, pre/code/script/style blocks and tags (so attributes too).
function visibleText(html) {
  return html
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<(pre|code|script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, ' ')
    .replace(/<[^>]*>/g, ' ');
}

function parsePage(html) {
  const ids = new Set();
  for (const m of html.matchAll(/\sid\s*=\s*(?:"([^"]*)"|'([^']*)')/g)) {
    ids.add(decode(decodeEntities(m[1] ?? m[2])));
  }
  const hrefs = [];
  for (const m of html.matchAll(/<(?:a|link|area)\b[^>]*?\shref\s*=\s*(?:"([^"]*)"|'([^']*)')/gi)) {
    hrefs.push(decodeEntities(m[1] ?? m[2]));
  }
  return { ids, hrefs };
}

const count = (html, re) => [...html.matchAll(re)].length;

export async function checkDist(distDir, { base, mermaid, asides }) {
  const problems = [];
  const root = distDir;
  const base_ = base.replace(/\/+$/, '');
  const files = await listPages(root);
  const pages = new Map(); // site path ("a/b") -> {ids}
  const htmls = new Map();
  for (const f of files) {
    const html = await readFile(f, 'utf8');
    htmls.set(f, html);
    const rel = relative(root, f).split(sep).join('/');
    const key = rel === 'index.html' ? '' : rel.replace(/(?:^|\/)index\.html$/, '').replace(/\.html$/, '');
    pages.set(key, parsePage(html).ids);
  }

  const fileExists = async (p) => {
    try {
      return (await stat(join(root, p))).isFile();
    } catch {
      return false;
    }
  };

  let mermaidCount = 0;
  let asideCount = 0;

  for (const [f, html] of htmls) {
    const rel = relative(root, f).split(sep).join('/');
    const where = rel;
    const pageKey = rel === 'index.html' ? '' : rel.replace(/(?:^|\/)index\.html$/, '').replace(/\.html$/, '');

    // links
    for (const raw of parsePage(html).hrefs) {
      const href = raw.trim();
      if (!href || href === '#' || SKIP_HREF.test(href)) continue;
      const hashAt = href.indexOf('#');
      const beforeHash = hashAt < 0 ? href : href.slice(0, hashAt);
      const hash = hashAt < 0 ? '' : decode(href.slice(hashAt + 1));
      const pathPart = decode(beforeHash.split('?')[0]);

      let target; // path relative to dist, no leading slash
      if (pathPart === '') {
        target = pageKey;
      } else if (pathPart.startsWith('/')) {
        if (pathPart !== base_ && !pathPart.startsWith(base_ + '/')) {
          problems.push(`${where}: link ${href} — absolute path outside base ${base_}`);
          continue;
        }
        target = posix.normalize(pathPart.slice(base_.length) || '/').replace(/^\/+/, '');
      } else {
        const pageDir = pageKey === '' ? '.' : pageKey;
        // pages are <dir>/index.html, so relative links resolve against <dir>/
        const baseDir = rel.endsWith('index.html') ? pageDir : posix.dirname(rel);
        target = posix.normalize(posix.join(baseDir, pathPart));
      }
      target = target.replace(/\/+$/, '').replace(/^\.$/, '');
      if (target.startsWith('..')) {
        problems.push(`${where}: link ${href} — leaves the site`);
        continue;
      }
      if (/^(?:_astro|pagefind)(?:\/|$)/.test(target)) continue;

      let pageTarget = null;
      if (pages.has(target)) pageTarget = target;
      else if (target !== '' && (await fileExists(target))) {
        if (hash && target.endsWith('.html')) pageTarget = target.replace(/\.html$/, '');
      } else {
        problems.push(`${where}: link ${href} — no such page or file`);
        continue;
      }
      if (hash && !hash.startsWith(':~:') && pageTarget !== null && !pages.get(pageTarget)?.has(hash)) {
        problems.push(`${where}: link ${href} — no such anchor #${hash}`);
      }
    }

    // text
    const text = decodeEntities(visibleText(html));
    if (text.includes('[[')) problems.push(`${where}: raw wikilink "[[" left in text`);
    if (text.includes('[!')) problems.push(`${where}: raw callout "[!" left in text`);

    // mermaid / asides
    if (/language-mermaid|data-language\s*=\s*["']mermaid["']/.test(html)) {
      problems.push(`${where}: mermaid went to code highlighting (language-mermaid)`);
    }
    mermaidCount += count(html, /<pre\b[^>]*\sclass\s*=\s*"(?:[^"]*\s)?mermaid(?:\s[^"]*)?"/gi);
    asideCount += count(html, /<aside\b[^>]*\sclass\s*=\s*"(?:[^"]*\s)?starlight-aside(?:\s[^"]*)?"/gi);
  }

  if (mermaidCount !== mermaid) problems.push(`mermaid blocks: expected ${mermaid}, got ${mermaidCount}`);
  if (asideCount !== asides) problems.push(`asides: expected ${asides}, got ${asideCount}`);
  return problems;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const opt = (name) => {
    const i = args.indexOf(name);
    return i < 0 ? undefined : args[i + 1];
  };
  const dir = args[0];
  const base = opt('--base');
  const mermaid = Number(opt('--mermaid'));
  const asides = Number(opt('--asides'));
  if (!dir || dir.startsWith('--') || base === undefined || Number.isNaN(mermaid) || Number.isNaN(asides)) {
    console.error('usage: check-dist.mjs <distDir> --base /reference --mermaid N --asides N');
    process.exit(2);
  }
  const problems = await checkDist(dir, { base, mermaid, asides });
  for (const p of problems) console.log(p);
  process.exit(problems.length ? 1 : 0);
}
