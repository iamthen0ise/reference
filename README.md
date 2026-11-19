# reference

Конспекты в формате Obsidian (`[[вики-ссылки]]`, callout-блоки, mermaid). Сайт собирается из vault через Astro Starlight и публикуется на GitHub Pages: https://iamthen0ise.github.io/reference/

Код сайта лежит в скрытом каталоге `.site/`, чтобы Obsidian не индексировал `node_modules`.

## Новые заметки

- Файл `.md` кладётся в папку раздела.
- Имя файла уникально во всём vault.
- Заметку нужно добавить в список `sidebar` в `.site/astro.config.mjs`, иначе её нет в меню; `make check` напомнит.
- Заголовки и ссылки пишутся в формате Obsidian; alias-пайп внутри таблицы экранируется: `\|`.

## Команды

- `make init` — установить зависимости и Chromium (сборка пререндерит mermaid).
- `make run` — локальный просмотр.
- `make check` — проверка vault, сборка и проверка результата; запускать перед пушем.
- `make init-browser` и `make check-browser` — браузерная проверка (нужна готовая сборка, `make build`).

## Деплой

Автоматически при пуше в `main` (`.github/workflows/deploy.yml`). Первый раз: в репозитории Settings → Pages → Source: **GitHub Actions**.
