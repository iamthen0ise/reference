import { readdir, readFile, stat } from 'node:fs/promises';
import { join, relative, sep, posix } from 'node:path';
import { fileURLToPath } from 'node:url';
import { countVault } from './check-vault.mjs';

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
    // tags with quoted attribute values: Expressive Code puts raw ">" in data-code="..."
    .replace(/<\/?[a-z][^\s>\/]*(?:"[^"]*"|'[^']*'|[^>"'])*>/gi, ' ');
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

// Ссылки бокового меню Starlight (<nav class="sidebar">) -> ключи страниц ("notes/a/b").
function sidebarKeys(html, base_) {
  const m = /<nav\b[^>]*\sclass\s*=\s*"(?:[^"]*\s)?sidebar(?:\s[^"]*)?"[^>]*>([\s\S]*?)<\/nav>/i.exec(html);
  if (!m) return null;
  const keys = new Set();
  for (const h of parsePage(m[1]).hrefs) {
    const path = decode(h.split('#')[0].split('?')[0]);
    if (path !== base_ && !path.startsWith(base_ + '/')) continue;
    keys.add(posix.normalize(path.slice(base_.length) || '/').replace(/^\/+|\/+$/g, ''));
  }
  return keys;
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
    if (/<pre\b[^>]*\sclass\s*=\s*"(?:[^"]*\s)?mermaid(?:\s[^"]*)?"/i.test(html)) {
      problems.push(`${where}: unrendered mermaid source (pre.mermaid)`);
    }
    // starlight-obsidian renders diagrams at build time: <picture><source id="mermaid-dark-N" ...><img id="mermaid-N" ...>
    mermaidCount += count(html, /<picture>\s*<source\b[^>]*\sid\s*=\s*"mermaid-dark-\d+"/gi);
    asideCount += count(html, /<aside\b[^>]*\sclass\s*=\s*"(?:[^"]*\s)?starlight-aside(?:\s[^"]*)?"/gi);
  }

  // Меню в astro.config.mjs написано вручную: новая заметка, не попавшая в него, недоступна из навигации.
  if (pages.has('notes')) {
    const keys = sidebarKeys(htmls.get(join(root, 'notes', 'index.html')), base_);
    // Заглушки redirects из astro.config.mjs (старые адреса переименованных заметок) в меню не нужны.
    const isRedirect = (k) => /<meta\b[^>]*\shttp-equiv\s*=\s*["']?refresh/i.test(htmls.get(join(root, k, 'index.html')) ?? '');
    const notePages = [...pages.keys()].filter((k) => k.startsWith('notes/') && !isRedirect(k));
    if (keys === null) {
      if (notePages.length) problems.push('notes/index.html: sidebar navigation not found');
    } else {
      for (const k of notePages) {
        if (!keys.has(k)) problems.push(`${k}/: not linked from the sidebar (add it to the sidebar in .site/astro.config.mjs)`);
      }
    }
  }

  if (mermaidCount !== mermaid) problems.push(`mermaid blocks: expected ${mermaid}, got ${mermaidCount}`);
  if (asideCount !== asides) problems.push(`asides: expected ${asides}, got ${asideCount}`);
  return problems;
}

// Разбор аргументов CLI. Ожидаемые числа: явные --mermaid/--asides или из vault (--vault <dir>); иначе null (usage).
export async function resolveOptions(args) {
  const opt = (name) => {
    const i = args.indexOf(name);
    return i < 0 ? undefined : args[i + 1];
  };
  const dir = args[0];
  const base = opt('--base');
  if (!dir || dir.startsWith('--') || base === undefined) return null;
  let mermaid = opt('--mermaid') === undefined ? undefined : Number(opt('--mermaid'));
  let asides = opt('--asides') === undefined ? undefined : Number(opt('--asides'));
  const vault = opt('--vault');
  if (vault !== undefined && (mermaid === undefined || asides === undefined)) {
    const counted = await countVault(vault);
    mermaid ??= counted.mermaid;
    asides ??= counted.asides;
  }
  if (mermaid === undefined || asides === undefined || Number.isNaN(mermaid) || Number.isNaN(asides)) return null;
  return { dir, base, mermaid, asides };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const o = await resolveOptions(process.argv.slice(2));
  if (!o) {
    console.error('usage: check-dist.mjs <distDir> --base /reference (--vault <vaultDir> | --mermaid N --asides N)');
    process.exit(2);
  }
  const problems = await checkDist(o.dir, o);
  for (const p of problems) console.log(p);
  process.exit(problems.length ? 1 : 0);
}
