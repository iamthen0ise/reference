---
title: JSON и JSONB
---

**Суть.** В Postgres два типа для JSON, и почти всегда нужен `jsonb`. Он хранит документ в разобранном бинарном виде, поэтому поля из него читаются быстро и по нему можно строить индексы. `json` хранит исходный текст и разбирает его заново при каждом обращении. jsonb подходит для полуструктурированных данных, у которых набор полей меняется от записи к записи. Но всё, по чему фильтруют, что соединяют через JOIN и на что нужны ограничения, лучше держать в обычных колонках: там у базы есть типы, статистика и `NOT NULL`.

|  | `json` | `jsonb` |
| --- | --- | --- |
| Хранение | текст как есть | разобранное бинарное дерево |
| Запись | быстрее (только проверка корректности) | чуть медленнее (нужен разбор) |
| Чтение полей | разбор при каждом обращении | быстро |
| Пробелы, порядок ключей, дубли ключей | сохраняются | не сохраняются (при дубле побеждает последний) |
| GIN-индекс | нет | да |
| Когда | нужна копия байт в байт (подпись вебхука, аудит) | всё остальное |

## Техника: операторы и индексы

```sql
CREATE TABLE products (
  id bigint PRIMARY KEY,
  category_id int NOT NULL,          -- по ней фильтруем и соединяем, поэтому отдельная колонка
  price numeric(12,2) NOT NULL,      -- по ней сортируем и считаем, поэтому отдельная колонка
  attrs jsonb NOT NULL DEFAULT '{}'  -- характеристики, разные для разных категорий
);

SELECT attrs -> 'screen' ->> 'size' FROM products;           -- -> возвращает jsonb, ->> возвращает text
SELECT attrs #>> '{screen,size}' FROM products;               -- значение по пути
SELECT * FROM products WHERE attrs @> '{"color": "black"}';    -- containment: документ содержит этот фрагмент
SELECT * FROM products WHERE attrs ? 'warranty';              -- ключ существует
SELECT * FROM products WHERE attrs @? '$.ram ? (@ >= 16)';    -- jsonpath: язык запросов по пути в документе
SELECT * FROM products WHERE (attrs ->> 'ram')::int >= 16;     -- с приведением типа

UPDATE products SET attrs = jsonb_set(attrs, '{color}', '"white"') WHERE id = 1;
UPDATE products SET attrs = attrs || '{"promo": true}' WHERE id = 1;   -- слияние документов
UPDATE products SET attrs = attrs - 'promo' WHERE id = 1;              -- удалить ключ

-- Индексы
CREATE INDEX ON products USING gin (attrs);                 -- jsonb_ops: @>, ?, ?|, ?&, @?, @@
CREATE INDEX ON products USING gin (attrs jsonb_path_ops);  -- меньше и быстрее, но только для @>, @? и @@
CREATE INDEX ON products ((attrs ->> 'brand'));             -- B-tree по одному полю: =, ORDER BY

-- Проверка структуры
ALTER TABLE products ADD CONSTRAINT attrs_is_object CHECK (jsonb_typeof(attrs) = 'object');

-- Выносим «горячее» поле в настоящую колонку.
-- STORED переписывает таблицу под ACCESS EXCLUSIVE — на большой таблице делать через expand/contract
ALTER TABLE products ADD COLUMN brand text GENERATED ALWAYS AS (attrs ->> 'brand') STORED;
```

> [!warning] Ловушка
> GIN-индекс по `attrs` **не** ускорит запрос `attrs ->> 'brand' = 'Apple'`. Нужно либо переписать условие как `@> '{"brand":"Apple"}'`, либо построить индекс по выражению.

## Техника: что возвращают операторы, GIN-индекс и ловушки

Данные для примеров: `products` на 200 тыс. строк, в `attrs` лежат `brand`, `color`, `ram` и вложенный `screen`; ключ `warranty` есть у каждой тысячной строки (200 штук).

```sql
SELECT id, attrs FROM products WHERE id IN (1, 1000) ORDER BY id;
```

| id | attrs |
| --- | --- |
| 1 | {"ram": 8, "brand": "Samsung", "color": "white", "screen": {"size": 5.6}} |
| 1000 | {"ram": 4, "brand": "Apple", "color": "black", "screen": {"size": 5.5}, "warranty": 36} |

Порядок ключей в jsonb не тот, в каком их записали: он хранит их по длине и алфавиту. Теперь операторы доступа на строке `id = 1`: `->` возвращает `jsonb`, `->>` возвращает `text`, `#>>` идёт по пути и тоже возвращает `text`:

```sql
SELECT attrs -> 'screen' AS j, attrs -> 'screen' ->> 'size' AS t, attrs #>> '{screen,size}' AS path,
       attrs -> 'brand' AS brand_j, attrs ->> 'brand' AS brand_t
FROM products WHERE id = 1;
```

| j | t | path | brand_j | brand_t |
| --- | --- | --- | --- | --- |
| {"size": 5.6} | 5.6 | 5.6 | "Samsung" | Samsung |

Разница в кавычках: `brand_j` — это jsonb-строка `"Samsung"` вместе с кавычками, `brand_t` — обычный текст. Отсюда первая ловушка, сравнение через `->`:

```sql
SELECT count(*) FROM products WHERE attrs -> 'brand' = 'Apple';
```

```text title="Вывод"
ERROR:  invalid input syntax for type json
DETAIL:  Token "Apple" is invalid.
```

Справа от `=` должен стоять jsonb, и литерал `'Apple'` Postgres разбирает как JSON, а `Apple` без кавычек — не JSON. Правильно либо `attrs -> 'brand' = '"Apple"'` (jsonb-строка), либо `attrs ->> 'brand' = 'Apple'` (текст); оба дают 40000. Вторая ловушка — сравнение чисел через `->>`:

```sql
SELECT count(*) FILTER (WHERE attrs ->> 'ram' > '16')         AS as_text,
       count(*) FILTER (WHERE (attrs ->> 'ram')::int > 16)    AS as_int,
       count(*) FILTER (WHERE attrs -> 'ram' > '16')          AS as_jsonb
FROM products;
```

| as_text | as_int | as_jsonb |
| --- | --- | --- |
| 150000 | 50000 | 50000 |

`->>` возвращает текст, а строки сравниваются посимвольно: `'4' > '16'` и `'8' > '16'` истинны (по первому символу), поэтому оперативная память в 4 и 8 ГБ «больше» 16. Ошибки нет, просто неверный ответ. Нужно приводить к числу (`::int`) или сравнивать `->` с jsonb-числом (`'16'` справа разбирается как JSON-число).

Containment и проверка ключа, и как меняется документ (исходный документ строки `id = 1`: `{"ram": 8, "brand": "Samsung", "color": "white", ...}`):

```sql
SELECT count(*) FROM products WHERE attrs @> '{"color": "black", "brand": "Apple"}';   -- 10000
SELECT count(*) FROM products WHERE attrs ? 'warranty';                                -- 200

SELECT jsonb_set(attrs, '{color}', '"black"') AS jsonb_set_result,
       attrs || '{"promo": true}'              AS merged,
       attrs - 'color'                         AS removed
FROM products WHERE id = 1;
```

| jsonb_set_result | merged | removed |
| --- | --- | --- |
| {"ram": 8, "brand": "Samsung", "color": "black", "screen": {"size": 5.6}} | {"ram": 8, "brand": "Samsung", "color": "white", "promo": true, "screen": {"size": 5.6}} | {"ram": 8, "brand": "Samsung", "screen": {"size": 5.6}} |

Все три выражения возвращают новый документ; `UPDATE` нужен, чтобы записать его в строку. Теперь GIN-индекс. Редкий ключ без индекса — Seq Scan по всей таблице:

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM products WHERE attrs @> '{"warranty": 36}';
```

```text title="Вывод"
Seq Scan on products  (cost=0.00..6275.00 rows=200 width=120) (actual time=0.057..14.157 rows=200 loops=1)
  Filter: (attrs @> '{"warranty": 36}'::jsonb)
  Rows Removed by Filter: 199800
  Buffers: shared hit=3775
Execution Time: 14.171 ms
```

```sql
CREATE INDEX idx_products_attrs ON products USING gin (attrs);
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM products WHERE attrs @> '{"warranty": 36}';
```

```text title="Вывод"
Bitmap Heap Scan on products  (cost=92.36..741.90 rows=200 width=120) (actual time=0.022..0.074 rows=200 loops=1)
  Recheck Cond: (attrs @> '{"warranty": 36}'::jsonb)
  Heap Blocks: exact=200
  Buffers: shared hit=203
  ->  Bitmap Index Scan on idx_products_attrs  (cost=0.00..92.31 rows=200 width=0) (actual time=0.013..0.013 rows=200 loops=1)
        Index Cond: (attrs @> '{"warranty": 36}'::jsonb)
        Buffers: shared hit=3
Execution Time: 0.080 ms
```

С индексом: 203 страницы вместо 3775, 0,08 мс вместо 14 мс. Размер индекса 2,3 МБ при таблице в 29 МБ. И ловушка из предупреждения выше, в плане: тот же поиск через `->>` индекс игнорирует.

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM products WHERE attrs ->> 'warranty' = '36';
```

```text title="Вывод"
Seq Scan on products  (cost=0.00..6775.00 rows=1000 width=120) (actual time=0.034..6.394 rows=200 loops=1)
  Filter: ((attrs ->> 'warranty'::text) = '36'::text)
  Rows Removed by Filter: 199800
  Buffers: shared hit=3775
Execution Time: 6.402 ms
```

Оценки расходятся с фактом: `rows=1000` против фактических 200. У планировщика нет статистики по ключам внутри документа, поэтому для `->> ... = ...` он берёт долю по умолчанию (0,5% таблицы), а для `@>` другую (0,1%, то есть 200 строк, что здесь совпало с фактом случайно). На реальных данных оценка по jsonb-ключам часто промахивается, и это ещё одна причина выносить важные поля в колонки.

## Когда jsonb — да, а когда нет

jsonb удобен, но им легко подменить продуманную схему. Слева — случаи, где он на своём месте, справа — где лучше обычные колонки.

| Хорошо подходит | Плохо подходит |
| --- | --- |
| атрибуты, разные для разных типов сущностей (каталог) | поля для JOIN, внешних ключей, агрегатов и сортировки |
| сырые тела запросов внешних API, вебхуки | денежные суммы, статусы и всё, что требует `NOT NULL` или `CHECK` |
| настройки пользователя, feature flags (флаги включения функций) | частые точечные изменения большого документа: он переписывается целиком, а значения больше 2 КБ уходят в TOAST |
| история и аудит изменений, события, тело сообщений outbox | «схема пока не ясна» как повод не проектировать схему |
| денормализованный снимок (состав заказа на момент покупки) | сложная аналитика: у планировщика нет статистики по ключам внутри документа (оценка около 0,1%) |

Outbox — таблица, в которую в той же транзакции записывают исходящие события, чтобы потом отдельный процесс надёжно отправил их в брокер (подробнее — [[Когда двигать offset#Смежное: transactional outbox (таблица исходящих событий)]]). TOAST (The Oversized-Attribute Storage Technique) — механизм Postgres, который сжимает большие значения и выносит их в отдельную служебную таблицу.

## Бизнес-кейсы

- **Маркетплейс с сотнями категорий.** У телефона есть диагональ и объём памяти, у платья — размер и ткань, и общего набора полей нет. Классических вариантов три. EAV (Entity-Attribute-Value) — таблица «сущность, атрибут, значение», где каждая характеристика лежит отдельной строкой: схема гибкая, но любой запрос превращается в цепочку JOIN. Отдельная таблица на каждую категорию даёт строгую схему, но каждая новая категория требует миграции. jsonb гибок и обходится без миграций, но проверять структуру приходится в приложении. Обычно выбирают так: атрибуты хранят в jsonb, допустимые атрибуты каждой категории описывают в отдельном справочнике и по нему проверяют данные, а фасетный поиск (фильтры со счётчиками) выносят в Elasticsearch.
- **Вебхуки платёжного провайдера.** Сырое тело запроса сохраняют как есть: оно понадобится при разборе спора с провайдером и при повторной обработке. Но `payment_id`, `status` и `amount` сразу вытаскивают в отдельные колонки, потому что по ним ищут платежи и сверяют суммы.
- **«Давайте хранить весь заказ одним JSON, как в MongoDB».** Первые месяцы это ускоряет разработку. Но через полгода финансы просят отчёт «выручка по SKU (артикулу товара) за квартал», и его приходится собирать, разворачивая миллионы документов: дорого и легко ошибиться. Компромисс такой: строки заказа хранят обычной таблицей, а в jsonb кладут снимок карточки товара на момент покупки, чтобы старый заказ не менялся, когда потом поменяют товар.

## Что спросят дальше

- **Что такое TOAST?** Postgres хранит строки страницами по 8 КБ, и большое значение в такую страницу не помещается. Поэтому значения больше примерно 2 КБ он сжимает и выносит в отдельную служебную таблицу, а в строке оставляет ссылку. Для jsonb из этого следует неприятное: чтобы прочитать одно поле большого документа, базе нужно достать и распаковать весь документ.
- **Postgres или MongoDB?** Postgres — если нужны транзакции, которые затрагивают несколько сущностей сразу, JOIN и строгие связи между таблицами. MongoDB — если каждый документ самодостаточен (его читают и пишут целиком) и нужно встроенное шардирование, то есть разнесение данных по серверам.
