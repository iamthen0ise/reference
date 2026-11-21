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
const page = (...path) => ({ slug: ['notes', ...path].join('/').toLowerCase().replaceAll(' ', '-').replaceAll(',', '') });
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
  {
    label: 'Python',
    items: [
      page('Python/Python'),
      {
        label: 'Язык',
        items: pages('Python/Язык', [
          'Типы и изменяемость', 'Области видимости и замыкания', 'Исключения', 'Импорт и модули',
          'Типизация и модели данных', 'Что нового в Python',
        ]),
      },
      {
        label: 'Функции и протоколы',
        items: pages('Python/Функции и протоколы', ['Декораторы', 'Итераторы и генераторы', 'Контекстные менеджеры']),
      },
      {
        label: 'Классы и объекты',
        items: pages('Python/Классы и объекты', ['Создание объектов', 'MRO и super', 'Атрибуты и дескрипторы']),
      },
      {
        label: 'Конкурентность',
        items: pages('Python/Конкурентность', [
          'Потоки и процессы', 'Память и сборка мусора', 'asyncio под капотом', 'asyncio на практике',
          'Ошибки и отмена в asyncio', 'Таймауты и нагрузка в asyncio',
        ]),
      },
      {
        label: 'Прикладное',
        items: pages('Python/Прикладное', ['Деньги и числа', 'Логирование', 'pytest и моки', 'SQLAlchemy async']),
      },
      {
        label: 'Рецепты',
        items: pages('Python/Рецепты', ['Рецепты', 'Выбор примитива синхронизации', 'Починка декораторов', 'Декоратор замера', 'Потерянные исключения в async', 'Скачать много ссылок', 'Список, генератор или async-генератор']),
      },
    ],
  },
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
              'PostgreSQL', 'Нормализация', 'Транзакции и уровни изоляции', 'Индексы', 'EXPLAIN', 'Миграции без простоя',
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
          { label: 'SQL-рецепты', items: pages('Бэкенд/Базы данных/SQL-рецепты', ['SQL-рецепты', 'Схема библиотеки', 'Запросы и индексы библиотеки', 'Множества в библиотеке', 'Эффективность индексов', 'Пожар в PostgreSQL', 'Отладка медленного запроса']) },
          {
            label: 'Redis',
            items: pages('Бэкенд/Базы данных/Redis', [
              'Redis', 'Структуры данных Redis', 'Кэширование в Redis', 'Распределённая блокировка в Redis',
              'Ограничение частоты запросов в Redis', 'Атомарность в Redis', 'Очереди и события в Redis',
              'Идемпотентность в Redis', 'Персистентность и память Redis', 'Репликация и кластер Redis',
            ]),
          },
        ],
      },
      {
        label: 'Очереди',
        items: [
          {
            label: 'Kafka',
            items: pages('Бэкенд/Очереди/Kafka', [
              'Kafka', 'Продюсер Kafka на практике', 'Консьюмер и группы Kafka', 'Порядок событий',
              'Когда двигать offset', 'Повторные попытки при падении базы', 'Retry-топики и DLQ в Kafka',
              'Транзакции Kafka и exactly-once', 'Схема событий и compaction', 'Распределённые транзакции и саги',
              'Эксплуатация Kafka и CLI',
            ]),
          },
          {
            label: 'RabbitMQ',
            items: pages('Бэкенд/Очереди/RabbitMQ', [
              'RabbitMQ', 'Exchange и маршрутизация', 'Надёжная доставка в RabbitMQ', 'Типы очередей RabbitMQ',
              'Повторы и DLQ в RabbitMQ', 'Конкурентность и порядок в RabbitMQ', 'RPC через RabbitMQ',
              'Эксплуатация RabbitMQ',
            ]),
          },
          page('Бэкенд/Очереди/Celery'),
        ],
      },
    ],
  },
  { label: 'Linux', items: pages('Linux', ['Linux', 'Процессы в Linux', 'Сигналы в Linux', 'Память в Linux', 'Файлы и дескрипторы', 'Ввод-вывод и сеть', 'Контейнеры изнутри']) },
  { label: 'Docker и Kubernetes', items: pages('Docker и Kubernetes', ['Образы Docker', 'Объекты Kubernetes', 'Конфигурация и нагрузки в Kubernetes', 'Деплой в Kubernetes', 'Ресурсы и остановка в Kubernetes', 'Отладка в Kubernetes', 'Отладка сервисов и нагрузки в Kubernetes']) },
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
