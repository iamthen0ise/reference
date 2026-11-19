import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightObsidian from 'starlight-obsidian';
import { satteri } from '@astrojs/markdown-satteri';
import { STARLIGHT_IGNORE } from './scripts/vault-ignore.mjs';
import rehypeBaseLinks from './src/plugins/rehype-base-links.mjs';

const base = '/reference';

// До отрисовки: палитра из localStorage -> data-palette. Любой сбой хранилища (в т.ч. SecurityError при самом
// обращении к localStorage) гасится; тогда подставляется безопасная заглушка, чтобы штатные скрипты Starlight
// (ThemeProvider/ThemeSelect читают localStorage без try/catch) не падали.
const paletteScript = `(function(){var d=document.documentElement,p='starlight';try{var v=localStorage.getItem('palette');if(v==='catppuccin'||v==='nord'||v==='flexoki')p=v}catch(e){try{Object.defineProperty(window,'localStorage',{configurable:true,value:{getItem:function(){return null},setItem:function(){},removeItem:function(){}}})}catch(e2){}}d.dataset.palette=p})()`;

// Страницы генерирует starlight-obsidian в notes/ (slug = путь в нижнем регистре, пробелы -> дефисы).
// Порядок меню задан явно: autogenerate сортировал бы по алфавиту, а служебных полей в заметки добавлять не хочется.
const page = (...path) => ({ slug: ['notes', ...path].join('/').toLowerCase().replaceAll(' ', '-') });
const pages = (dir, names) => names.map((name) => page(dir, name));
const sidebar = [
  { label: 'Оглавление', slug: 'notes' },
  {
    label: 'Алгоритмы',
    items: [
      {
        label: 'Паттерны задач',
        items: pages('Алгоритмы/Паттерны задач', [
          'Паттерны задач', 'Два указателя', 'Плавающее окно', 'Хеш-таблица', 'Точки и отрезки',
          'Бинарный поиск', 'Стек', 'Префиксный массив', 'Односвязный список', 'Перебор комбинаций',
          'Деревья', 'Графы', 'Динамическое программирование',
        ]),
      },
      page('Алгоритмы/Структуры данных'),
      { label: 'Сортировки', items: pages('Алгоритмы/Сортировки', ['Сортировки', 'Сортировки для эрудиции']) },
    ],
  },
  { label: 'Python', items: pages('Python', ['Python', 'Python-протоколы', 'Python-классы', 'Python-конкурентность']) },
  {
    label: 'Бэкенд',
    items: [
      page('Бэкенд/Чек-лист бэкенда'),
      {
        label: 'Базы данных',
        items: [
          {
            label: 'PostgreSQL',
            items: pages('Бэкенд/Базы данных/PostgreSQL', [
              'PostgreSQL', 'Транзакции и уровни изоляции', 'Индексы', 'EXPLAIN', 'Миграции без простоя',
              'JSON и JSONB', 'Блокировки', 'Эксплуатация PostgreSQL',
            ]),
          },
          {
            label: 'SQL-задачи',
            items: pages('Бэкенд/Базы данных/SQL-задачи', [
              'SQL-задачи', 'JOIN и подзапросы', 'UNION INTERSECT EXCEPT', 'Агрегация', 'Оконные функции',
              'Классические SQL-задачи', 'CTE и LATERAL', 'CASE COALESCE NULLIF', 'Ловушки SQL',
            ]),
          },
          page('Бэкенд/Базы данных/Redis'),
        ],
      },
      {
        label: 'Очереди',
        items: [
          {
            label: 'Kafka',
            items: pages('Бэкенд/Очереди/Kafka', [
              'Kafka', 'Порядок событий', 'Повторные попытки при падении базы', 'Когда двигать offset',
              'Схема событий и compaction', 'Распределённые транзакции и саги',
            ]),
          },
          page('Бэкенд/Очереди/RabbitMQ'),
          page('Бэкенд/Очереди/Celery'),
        ],
      },
    ],
  },
];

export default defineConfig({
  site: 'https://iamthen0ise.github.io',
  base,
  // Главная — оглавление из index.md, которое starlight-obsidian кладёт в /notes/.
  redirects: { '/': `${base}/notes/` },
  markdown: {
    // Astro 7 рендерит markdown через Sätteri; starlight-obsidian пишет ссылки /notes/... без base.
    processor: satteri({ hastPlugins: [rehypeBaseLinks({ base })] }),
  },
  integrations: [
    starlight({
      title: 'Reference',
      locales: { root: { label: 'Русский', lang: 'ru' } },
      favicon: '/favicon.svg',
      customCss: ['./src/styles/base.css', './src/styles/palettes.css', './src/styles/mobile.css'],
      components: { ThemeSelect: './src/components/ThemeSelect.astro' },
      head: [{ tag: 'script', content: paletteScript }],
      plugins: [
        starlightObsidian({
          vault: '..',
          // Заглушка вместо .obsidian/, которого нет в репозитории (и в CI).
          configFolder: '.site/vault-config',
          // Заголовок из frontmatter (title: Reference у главной), а не имя файла.
          copyFrontmatter: 'starlight',
          // Тот же список, что у check-vault (scripts/vault-ignore.mjs).
          ignore: STARLIGHT_IGNORE,
        }),
      ],
      sidebar,
    }),
  ],
});
