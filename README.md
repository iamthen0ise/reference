# reference

Конспекты в Markdown (формат Obsidian: `[[вики-ссылки]]`, callout-блоки, mermaid). Публикуются на GitHub Pages через [Quartz](https://quartz.jzhao.xyz): https://iamthen0ise.github.io/reference/

- Новая заметка — `.md` в корне; ссылка на неё из `index.md`.
- Деплой — автоматически при пуше в `main` (`.github/workflows/deploy.yml`).
- Настройки сайта — `site/quartz.config.ts` и `site/quartz.layout.ts` (без graph view) и `site/custom.scss` (стили, mobile first).
- Первый раз: в репозитории Settings → Pages → Source: **GitHub Actions**.

## Локальный просмотр

```sh
git clone https://github.com/jackyzha0/quartz.git /tmp/quartz && git -C /tmp/quartz checkout d25a6ea
cp site/quartz.*.ts /tmp/quartz/ && cp site/custom.scss /tmp/quartz/quartz/styles/ && rm -rf /tmp/quartz/content && mkdir /tmp/quartz/content && cp *.md /tmp/quartz/content/ && rm /tmp/quartz/content/README.md
cd /tmp/quartz && npm ci && npx quartz build --serve
```
