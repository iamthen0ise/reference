# Переезд конспектов на Astro Starlight — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** заметки разложены по папкам, крупные разрезаны на страницы, сайт собирается Astro Starlight с меню из папок, оглавлением, поиском и переключателем палитр и деплоится на GitHub Pages.

**Architecture:** заметки остаются в vault (корень репозитория) и правятся в Obsidian. Astro-проект в `.site/` читает vault плагином `starlight-obsidian`, а mermaid рендерит `astro-mermaid`. Корректность проверяют две утилиты без зависимостей: `check-vault` проверяет, что все вики-ссылки в vault резолвятся, `check-dist` проверяет, что в собранном сайте нет битых ссылок, якорей и сырого синтаксиса Obsidian. Обе запускаются через `make check` локально и в CI.

**Tech Stack:** Astro 7.4, Starlight 0.43, starlight-obsidian 0.15, astro-mermaid 2.2 (mermaid 11), Node 22 (CI), `node --test`, Playwright (только локальная браузерная проверка), Make.

**Spec:** `docs/superpowers/specs/2026-11-19-astro-starlight-design.md`. Исполнитель читает её целиком, особенно «Структура», «Ссылки» и «Уточнения после ревью».

## Global Constraints

- Работа ведётся в ветке `astro-starlight` от `main`. Push и merge — только после «да» пользователя.
- Vault — корень репозитория. В сайт не попадают любые скрытые каталоги (`.*`), `.site/`, `docs/`, `.github/`, `.obsidian/`, `README.md`, `node_modules/`.
- `site: 'https://iamthen0ise.github.io'`, `base: '/reference'`, язык интерфейса `ru`, заголовок «Reference».
- Шрифты: Inter (текст) и JetBrains Mono (код), обязательно с кириллицей. Подключаются self-hosted через `@fontsource` с подмножеством cyrillic.
- Node 22 в CI, `.site/package-lock.json` лежит в git, версии задаются точными диапазонами `~`.
- Все процедуры оформлены целями корневого `Makefile`: `init`, `run`, `build`, `test`, `check`, `check-browser`.
- Утилиты проверки пишутся на чистом Node ESM (`.mjs`), без зависимостей; тесты — `node --test`.
- Числа-инварианты контента: 32 блока mermaid и 30 callout-блоков (`note` ×5, `tip` ×16, `warning` ×8, `info` ×1).
- Текст заметок не переписывается. Меняются только расположение файлов, уровни заголовков в вынесенных разделах, вики-ссылки и строки навигации «Разделы:».

## Review Focus

1. **Якоря с кириллицей и пунктуацией** («EXPLAIN vs EXPLAIN ANALYZE», «UNION, INTERSECT и EXCEPT», заголовки с `` `code` ``, «ё», скобками). Ожидание: `href` с percent-encoding ведёт на существующий `id`. Тест в Task 3 (`anchor: cyrillic percent-encoded href resolves`), прогон на реальной сборке в Task 4.
2. **`[[` и `[!` внутри кода.** В Python бывают вложенные списки `[[1, 2]]`, а синтаксис Obsidian может стоять в примерах. Ожидание: скрипт разрезки их не трогает, проверки не считают их ссылками. Тесты в Task 1 и Task 3 (`ignores code`).
3. **`#` внутри ограждённого кода** (комментарии в Kafka, Redis, Celery). Ожидание: такие строки не считаются заголовком, местом разреза или целью ссылки. Тест в Task 1 (`heading inside fence is not a heading`), инвариант числа ограждений в Task 2.
4. **Широкие таблицы и код на 390px.** Ожидание: страница не уезжает вбок, скроллится сам блок. Проверка в Task 5 (`check-browser`).
5. **Хранилище браузера недоступно** (приватный режим, запрет cookies). Ожидание: страница рисуется, палитра штатная, ошибок в консоли нет. Проверка в Task 5 (`check-browser`, сценарий с `localStorage`, который бросает исключение).

---

### Task 1: Проверка вики-ссылок в vault

Риск: обычный — инструмент проверки, данных не меняет.

**Files:**
- Create: `Makefile`
- Create: `.site/scripts/check-vault.mjs`
- Test: `.site/scripts/check-vault.test.mjs`, фикстуры `.site/scripts/fixtures/vault-ok/`, `.site/scripts/fixtures/vault-broken/`

**Interfaces:**
- Produces: CLI `node .site/scripts/check-vault.mjs <vaultDir>`. Код возврата 0, если всё резолвится, иначе 1. На каждую проблему печатается строка `<relpath>: [[<link>]] — <reason>`, где reason — `no such note`, `ambiguous note name` или `no such heading`.
- Produces: экспорт `checkVault(vaultDir: string): Promise<Array<{file: string, link: string, reason: string}>>`.
- Правила резолва повторяют Obsidian. Цель `[[Имя]]` ищется по basename `.md` среди файлов vault, без учёта игнорируемых каталогов из Global Constraints; имя должно быть уникальным. `[[Имя#Заголовок]]` требует заголовка с точно таким текстом (`#`…`######`) в целевом файле, `[[#Заголовок]]` — в текущем. Алиас `|…` отбрасывается. Ограждённые блоки кода (```` ``` ````, `~~~`) и инлайн-код пропускаются целиком, в том числе строки `#` внутри них. Frontmatter тоже пропускается.

- [ ] **Step 1: Создать ветку** — `git switch -c astro-starlight`.
- [ ] **Step 2: Написать фикстуры и падающие тесты** в `check-vault.test.mjs`:
  - `ok vault has no problems`: в `vault-ok/` лежат `A.md` (ссылки `[[B]]`, `[[B#Раздел два]]`, `[[B|алиас]]`, `[[#Свой]]`) и `sub/B.md` (`## Раздел два`) → `[]`.
  - `reports missing note, missing heading, ambiguous name`: в `vault-broken/` лежат `[[Нет]]`, `[[B#Нет такого]]` и два файла `x/C.md` и `y/C.md` со ссылкой `[[C]]` → ровно три проблемы с reason `no such note`, `no such heading`, `ambiguous note name`.
  - `ignores code`: ссылка `[[Нет]]` внутри ```` ```python ```` и внутри `` `инлайн` `` → проблем нет.
  - `heading inside fence is not a heading`: в `B.md` строка `# Фейк` стоит внутри ограждения, `[[B#Фейк]]` → `no such heading`.
  - `skips ignored dirs`: `.site/D.md` и `docs/D.md` не участвуют в резолве, `[[D]]` → `no such note`.
- [ ] **Step 3: Запустить** `node --test .site/scripts/` — ожидается FAIL (модуля нет).
- [ ] **Step 4: Реализовать** `checkVault` и CLI в `.site/scripts/check-vault.mjs`.
- [ ] **Step 5: Запустить** `node --test .site/scripts/` — ожидается PASS. Прогнать на текущем vault (`node .site/scripts/check-vault.mjs .`): ожидается 0 проблем. Если проблемы есть, это существующие битые ссылки: перечислить их в отчёте и исправить отдельным коммитом в этой же задаче.
- [ ] **Step 6: Makefile** — цели `.PHONY: test check-vault`; `test` = `node --test .site/scripts/`, `check-vault` = `node .site/scripts/check-vault.mjs .`.
- [ ] **Step 7: Commit** — `Add vault wiki-link checker`.

### Task 2: Раскладка по папкам и разрезка заметок

Риск: высокий — перенос и удаление контента; ошибка молча теряет текст.

**Files:**
- Move: все заметки по дереву из раздела спеки «Структура»
- Create: страницы разрезанных заметок (список в спеке)
- Modify: `index.md` и строки «Разделы:» / «Рядом:» в затронутых заметках
- One-off: `<scratchpad>/split.py` в scratchpad текущей сессии, **не** в репозитории

**Interfaces:**
- Consumes: `make check-vault` (Task 1).
- Produces: дерево vault ровно как в спеке. Файлы и `title` во frontmatter называются по таблице «Уточнения после ревью». Task 4 полагается на пути папок `Алгоритмы/`, `Python/`, `Бэкенд/Базы данных/`, `Бэкенд/Очереди/`.

Правила, которые скрипт обязан соблюдать (из спеки):
- Режутся ровно «Паттерны задач», PostgreSQL, SQL-задачи и Kafka; у «Сортировок» выносится только «Для эрудиции». Разрез идёт по `## ` вне ограждённого кода.
- Обзор = frontmatter + текст до первого `##` и разделы, перечисленные в спеке для обзора. Строка «Разделы:» в обзоре заменяется маркированным списком ссылок на подстраницы в исходном порядке.
- Вынесенный раздел: frontmatter `title: <имя файла>`, сам заголовок `##` удаляется, `###`→`##`, `####`→`###` (вне кода).
- «Пустой блок паттерна» удаляется: это шаблон, он остаётся в истории git.
- Ссылки переписываются по карте `(старый файл, заголовок) → (новый файл, заголовок | None)`. `## X` → `(Файл(X), None)`; заголовки внутри раздела X → `(Файл(X), заголовок)`; разделы обзора → `(Обзор, заголовок)`. `[[#H]]` внутри разрезанной заметки резолвится как `[[<старое имя>#H]]` и становится `[[#H]]`, если цель на той же странице. Алиас сохраняется. Код не трогается.

- [ ] **Step 1: Снять снимок «до»** во временный файл в scratchpad: для всех `.md` vault посчитать число ограждений ```` ``` ````, блоков ```` ```mermaid ```` (32) и строк `> [!` (30), а также собрать мультимножество непустых строк, которые не являются заголовком, не содержат `[[` и не начинаются с `Разделы:`/`Рядом:`.
- [ ] **Step 2: Написать и запустить `split.py`** (переносы делать через `git mv`, чтобы сохранилась история).
- [ ] **Step 3: Проверить инварианты** тем же скриптом снимка: ограждения, 32 mermaid и 30 callout совпадают. Мультимножество строк «после» ⊇ «до» за вычетом строк «Пустого блока паттерна». Разница должна состоять только из этих строк; любую другую разницу показать в отчёте.
- [ ] **Step 4: Запустить** `make check-vault` — ожидается 0 проблем. Также `make test` — PASS.
- [ ] **Step 5: Обновить `index.md`**: ссылки и описания под новую структуру, разделы «Алгоритмы / Python / Бэкенд» остаются. Снова `make check-vault` → 0.
- [ ] **Step 6: Commit** — `Split large notes and move notes into section folders`. В сообщение добавить список инвариантов и их значения.

### Task 3: Проверка собранного сайта

Риск: обычный — инструмент проверки.

**Files:**
- Create: `.site/scripts/check-dist.mjs`
- Test: `.site/scripts/check-dist.test.mjs`, фикстура `.site/scripts/fixtures/dist/` (несколько `index.html`)
- Modify: `Makefile`

**Interfaces:**
- Produces: CLI `node .site/scripts/check-dist.mjs <distDir> --base /reference --mermaid 32 --asides 30`. Код возврата 0/1, проблемы печатаются по строке.
- Produces: экспорт `checkDist(distDir: string, opts: {base: string, mermaid: number, asides: number}): Promise<string[]>`.
- Что проверяется:
  - каждый внутренний `href` (относительный или начинающийся с `base`) после `decodeURIComponent` указывает на существующий `index.html` или файл в `dist`;
  - `#якорь` совпадает с `id` на целевой странице, `id` тоже декодируются;
  - в тексте вне `<pre>`, `<code>` и `<script>` нет `[[` и `[!`;
  - нет `language-mermaid` / `data-language="mermaid"`, то есть mermaid не ушёл в подсветку кода;
  - число `<pre class="mermaid">` по всему сайту равно `--mermaid`, число `aside.starlight-aside` равно `--asides`.
  Внешние ссылки (`http(s)://` на другой хост) и `mailto:` не проверяются. Разбор — регулярками по атрибутам, без зависимостей. Служебные каталоги `dist/_astro/` и `dist/pagefind/` пропускаются.

- [ ] **Step 1: Написать фикстуры и падающие тесты:**
  - `clean dist passes` → `[]`;
  - `broken page link reported`;
  - `missing anchor reported`;
  - `anchor: cyrillic percent-encoded href resolves` — `href="/reference/a/#%D0%B8%D0%BD%D0%B4%D0%B5%D0%BA%D1%81%D1%8B"` и `id="индексы"` → OK;
  - `raw wikilink and callout in text reported`;
  - `ignores code` — `[[1, 2]]` и `[!tip]` внутри `<pre>`/`<code>` → OK;
  - `mermaid and aside counts enforced` — при неверном числе ожидается проблема с ожидаемым и фактическим значением.
- [ ] **Step 2: Запустить** `make test` — ожидается FAIL.
- [ ] **Step 3: Реализовать** `checkDist` и CLI.
- [ ] **Step 4: Запустить** `make test` — ожидается PASS.
- [ ] **Step 5: Makefile** — цель `check-dist` = `node .site/scripts/check-dist.mjs .site/dist --base /reference --mermaid 32 --asides 30`.
- [ ] **Step 6: Commit** — `Add built-site link and syntax checker`.

### Task 4: Сайт на Astro Starlight

Риск: обычный — сборка и конфиг; корректность ловят `check-vault` и `check-dist`.

**Files:**
- Create: `.site/package.json`, `.site/package-lock.json`, `.site/astro.config.mjs`, `.site/tsconfig.json`, `.site/src/content.config.ts`, `.site/src/styles/base.css`, `.gitignore`
- Modify: `Makefile`

**Interfaces:**
- Consumes: дерево vault из Task 2, `make check-dist` из Task 3.
- Produces: `npm run build` в `.site/` собирает сайт в `.site/dist/`. Каталог, куда `starlight-obsidian` генерирует страницы, попадает в `.gitignore`. Task 5 переопределяет компонент `ThemeSelect` и добавляет CSS в массив `customCss` в `astro.config.mjs`.

- [ ] **Step 1: Скаффолд** `.site/` с зависимостями `astro@~7.4`, `@astrojs/starlight@~0.43`, `starlight-obsidian@~0.15`, `astro-mermaid@~2.2`, `mermaid@~11`, `@fontsource/inter`, `@fontsource/jetbrains-mono`. Скрипты `dev`, `build`, `preview`.
- [ ] **Step 2: Конфиг:**
  - `starlightObsidian({ vault: '..', ignore: [...] })` — игнор из Global Constraints;
  - `astro-mermaid` с автопереключением светлой и тёмной темы, подключённый так, чтобы mermaid-блоки не попадали в Expressive Code (порядок интеграций — по README `astro-mermaid`);
  - `locales: { root: { label: 'Русский', lang: 'ru' } }`, `title: 'Reference'`, `site` и `base` из Global Constraints;
  - сайдбар: группы «Алгоритмы», «Python», «Бэкенд» (внутри «Чек-лист бэкенда», «Базы данных», «Очереди»). В разрезанной папке обзор идёт первым, затем подстраницы в порядке разделов исходной заметки. Механизм порядка выбирает исполнитель (явный `sidebar` или `sidebar.order`), но он не должен добавлять служебные поля в заметки, видимые в Obsidian, если без этого можно обойтись;
  - главная `/reference/` показывает содержимое `index.md` или перенаправляет на его страницу.
- [ ] **Step 3: Шрифты** — Inter 400/600/700 и JetBrains Mono 400 с подмножеством cyrillic, подключены через `customCss` в `base.css`; `--sl-font` и `--sl-font-mono` указывают на них.
- [ ] **Step 4: Makefile** — `init` = `cd .site && npm ci`, `run` = `cd .site && npm run dev`, `build` = `cd .site && npm run build`, `check` = `check-vault` → `build` → `check-dist`.
- [ ] **Step 5: Запустить** `make check`. Ожидается: сборка без предупреждений о битых ссылках, `check-dist` выдаёт 0 проблем.
  **Стоп-условие:** если `starlight-obsidian` не даёт пройти `check-dist` и это не чинится его настройками (якоря с кириллицей, алиасы, callout→aside, mermaid), не переходить на свои remark-плагины самостоятельно. Вернуть BLOCKED с конкретными падающими ссылками и причиной.
- [ ] **Step 6: Commit** — `Build notes site with Astro Starlight`.

### Task 5: Палитры, мобильная вёрстка, браузерная проверка

Риск: обычный — UI.

**Files:**
- Create: `.site/src/components/ThemeSelect.astro`, `.site/src/styles/palettes.css`, `.site/src/styles/mobile.css`, `.site/scripts/check-browser.mjs`
- Modify: `.site/astro.config.mjs` (`components.ThemeSelect`, `customCss`, `head`), `.site/package.json` (devDependency `playwright`), `Makefile`

**Interfaces:**
- Consumes: сборка из Task 4.
- Палитры: `starlight` (по умолчанию, без переопределений), `catppuccin`, `nord`, `flexoki`. Каждая задаёт `--sl-color-accent-low|accent|accent-high`, `--sl-color-white`, `--sl-color-gray-1…7`, `--sl-color-black` отдельно для `:root[data-palette="<id>"]` (тёмная) и `:root[data-palette="<id>"][data-theme="light"]`.
- Хранение: ключ `localStorage` — `palette`, атрибут `data-palette` на `<html>`. Инлайн-скрипт в `head` выставляет атрибут до отрисовки. Все обращения к `localStorage` обёрнуты в try/catch, при ошибке используется `starlight`.
- `ThemeSelect.astro` рендерит штатный `@astrojs/starlight/components/ThemeSelect.astro` и рядом `<select id="palette-select">` с подписями «Starlight», «Catppuccin», «Nord», «Flexoki».
- `check-browser.mjs` запускается против `astro preview` и проверяет:
  - (a) на всех страницах из `dist` при ширине 390px `document.documentElement.scrollWidth <= 390`;
  - (b) на страницах с mermaid число `svg` внутри `.mermaid` равно числу блоков, и в светлой, и в тёмной теме;
  - (c) выбор `nord` переживает перезагрузку: `data-palette="nord"`;
  - (d) если `localStorage.getItem` и `setItem` бросают исключение (через `addInitScript`), страница рисуется, `data-palette` = `starlight`, нет `pageerror`.
  Код возврата 0 или 1.

- [ ] **Step 1: Написать `check-browser.mjs`** и цель Makefile `check-browser`. Она поднимает `npm run preview` в фоне, запускает скрипт и гасит превью. Цель `init-browser` = `cd .site && npx playwright install chromium`.
- [ ] **Step 2: Запустить** `make build check-browser` — ожидается FAIL в (c), потому что переключателя ещё нет. В (a) возможны падения, они фиксируют текущее состояние мобильной вёрстки.
- [ ] **Step 3: Реализовать** палитры, `ThemeSelect`, инлайн-скрипт и `mobile.css`. В `mobile.css` переносятся правила из старого `site/custom.scss`: таблицы `display:block; overflow-x:auto`, ячейки `min-width: 9rem; max-width: 22rem`, переносы, `pre` со скроллом и `font-size: 0.8rem`, `overflow-wrap: anywhere` для текста статьи. Селекторы адаптируются под разметку Starlight (`.sl-markdown-content`).
- [ ] **Step 4: Запустить** `make check && make check-browser` — ожидается PASS.
- [ ] **Step 5: Commit** — `Add palette switcher and mobile layout fixes`.

### Task 6: Деплой, README, удаление Quartz

Риск: обычный — CI-конфиг и документация.

**Files:**
- Modify: `.github/workflows/deploy.yml`, `README.md`
- Delete: `site/quartz.config.ts`, `site/quartz.layout.ts`, `site/custom.scss`, затем пустой каталог `site/`

**Interfaces:**
- Consumes: цели Makefile `init` и `check`.

- [ ] **Step 1: Workflow:**
  - `actions/checkout@v4`;
  - `actions/setup-node@v4` с `node-version: 22`, `cache: npm`, `cache-dependency-path: .site/package-lock.json`;
  - `make init`, затем `make check`;
  - `actions/configure-pages@v5`, `actions/upload-pages-artifact@v3` с `path: .site/dist`;
  - job `deploy` без изменений. Клон Quartz и `QUARTZ_REF` удаляются.
- [ ] **Step 2: README** — сайт собирается Astro Starlight из vault. Новые заметки кладутся в папку раздела, имя файла должно быть уникальным во всём vault. Локально: `make init`, `make run`, перед пушем `make check`. Для браузерной проверки: `make init-browser`, `make check-browser`.
- [ ] **Step 3: Удалить файлы Quartz.** Запустить `make check` — ожидается PASS. Проверить, что синтаксис workflow корректен: `actionlint`, если он установлен, иначе внимательно перечитать.
- [ ] **Step 4: Commit** — `Deploy Astro site to GitHub Pages, drop Quartz`.
- [ ] **Step 5: Не пушить.** Сообщить главному потоку, что ветка готова к финальному ревью.
