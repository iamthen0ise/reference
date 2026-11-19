// Браузерная проверка собранного сайта против `astro preview`.
// Запуск: node check-browser.mjs --dist dist --url http://127.0.0.1:4321 --base /reference --mermaid 32
import { readdir, readFile } from 'node:fs/promises';
import { join, relative, sep } from 'node:path';
import { chromium } from 'playwright';

const args = {};
for (let i = 2; i < process.argv.length; i += 2) args[process.argv[i].replace(/^--/, '')] = process.argv[i + 1];
const dist = args.dist ?? 'dist';
const origin = args.url ?? 'http://127.0.0.1:4321';
const base = (args.base ?? '/reference').replace(/\/$/, '');
const expectedMermaid = args.mermaid === undefined ? null : Number(args.mermaid);
const SKIP_DIRS = new Set(['_astro', 'pagefind']);

async function listIndexPages(root, dir = root) {
  const out = [];
  for (const e of await readdir(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) {
      if (dir === root && SKIP_DIRS.has(e.name)) continue;
      out.push(...(await listIndexPages(root, p)));
    } else if (e.name === 'index.html') out.push(p);
  }
  return out;
}

// Редирект-заглушки (meta refresh, корень сайта) не страницы: у них нечего проверять.
const isRedirect = async (file) => /<meta\s+http-equiv="refresh"/i.test(await readFile(file, 'utf8'));
const files = [];
for (const f of (await listIndexPages(dist)).sort()) if (!(await isRedirect(f))) files.push(f);

const pages = files.map((file) => {
  const segs = relative(dist, file).split(sep).slice(0, -1);
  const path = `${base}/${segs.map(encodeURIComponent).join('/')}${segs.length ? '/' : ''}`;
  return { file, url: origin + path };
});

const problems = [];
const fail = (msg) => problems.push(msg);

const browser = await chromium.launch();
try {
  // (a) нет горизонтального скролла на 390px
  {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 800 } });
    const page = await ctx.newPage();
    for (const { url } of pages) {
      const res = await page.goto(url, { waitUntil: 'load' });
      if (!res?.ok()) { fail(`(a) ${url}: HTTP ${res?.status()}`); continue; }
      const sw = await page.evaluate(() => document.documentElement.scrollWidth);
      if (sw > 390) fail(`(a) ${url}: scrollWidth ${sw} > 390`);
    }
    await ctx.close();
    console.log(`(a) страниц на 390px: ${pages.length}`);
  }

  // (b) mermaid пререндерен в <picture>: все <img> загружены в светлой и тёмной схеме
  {
    let total = 0;
    const ctx = await browser.newContext({ viewport: { width: 1200, height: 800 } });
    const page = await ctx.newPage();
    const mermaidPictures = () =>
      [...document.querySelectorAll('picture')].filter((p) => p.querySelector('source[id^="mermaid-dark-"]'));
    for (const { url, file } of pages) {
      const html = await readFile(file, 'utf8');
      const count = (html.match(/<source\b[^>]*\bid="mermaid-dark-\d+"/g) ?? []).length;
      if (!count) continue;
      total += count;
      for (const scheme of ['light', 'dark']) {
        await page.emulateMedia({ colorScheme: scheme });
        await page.goto(url, { waitUntil: 'load' });
        await page.waitForFunction(
          (fn) => new Function(`return (${fn})()`)().every((p) => p.querySelector('img')?.complete),
          mermaidPictures.toString(), { timeout: 10000 },
        ).catch(() => {});
        const ok = await page.evaluate((fn) =>
          new Function(`return (${fn})()`)().map((p) => {
            const i = p.querySelector('img');
            return !!i && i.complete && i.naturalWidth > 0;
          }), mermaidPictures.toString());
        const good = ok.filter(Boolean).length;
        if (ok.length !== count || good !== count) fail(`(b) ${url} [${scheme}]: загружено ${good}/${ok.length}, в HTML ${count}`);
      }
    }
    if (expectedMermaid !== null && total !== expectedMermaid) fail(`(b) всего диаграмм ${total}, ожидалось ${expectedMermaid}`);
    console.log(`(b) диаграмм на сайте: ${total}`);
    await ctx.close();
  }

  // (c) выбор палитры переживает перезагрузку
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await page.goto(pages[0].url, { waitUntil: 'load' });
    const sel = page.locator('select.palette-select:visible').first();
    if (!(await sel.count())) fail('(c) нет видимого select.palette-select');
    else {
      await sel.selectOption('nord');
      const now = await page.evaluate(() => document.documentElement.dataset.palette);
      if (now !== 'nord') fail(`(c) после выбора data-palette="${now}"`);
      await page.reload({ waitUntil: 'load' });
      const after = await page.evaluate(() => document.documentElement.dataset.palette);
      if (after !== 'nord') fail(`(c) после перезагрузки data-palette="${after}"`);
      const val = await page.locator('select.palette-select:visible').first().inputValue();
      if (val !== 'nord') fail(`(c) после перезагрузки select="${val}"`);
    }
    await ctx.close();
  }

  // (d) localStorage бросает исключения: страница рисуется, палитра starlight, без pageerror
  {
    const ctx = await browser.newContext();
    await ctx.addInitScript(() => {
      const boom = () => { throw new DOMException('blocked', 'SecurityError'); };
      Storage.prototype.getItem = boom;
      Storage.prototype.setItem = boom;
    });
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.goto(pages[0].url, { waitUntil: 'load' });
    const palette = await page.evaluate(() => document.documentElement.dataset.palette);
    if (palette !== 'starlight') fail(`(d) data-palette="${palette}", ожидалось starlight`);
    const h1 = await page.locator('h1').first().innerText().catch(() => '');
    if (!h1.trim()) fail('(d) страница не отрисовалась (нет h1)');
    const sel = page.locator('select.palette-select:visible').first();
    if (await sel.count()) await sel.selectOption('nord').catch((e) => fail(`(d) selectOption: ${e.message}`));
    if (errors.length) fail(`(d) pageerror: ${errors.join('; ')}`);
    await ctx.close();
  }
} finally {
  await browser.close();
}

console.log(`проверено страниц: ${pages.length}`);
if (problems.length) {
  console.error(`check-browser: ${problems.length} проблем`);
  for (const p of problems.slice(0, 50)) console.error('  ' + p);
  process.exit(1);
}
console.log('check-browser: 0 проблем');
