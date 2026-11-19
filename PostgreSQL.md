---
title: PostgreSQL
---

Конспект к собеседованию по PostgreSQL: изоляция, индексы, планы запросов, миграции без простоя, jsonb, блокировки и эксплуатация. В каждой теме четыре слоя: **суть** (ответ на 30 секунд), **техника** (SQL), **бизнес-кейс** и **что спросят дальше**. Примеры — для PostgreSQL 14+, отличия новых версий (до 18) отмечены; код приложения — Python, SQLAlchemy 2.0 async.
Рядом: [[SQL-задачи]] (задачи на запросы: JOIN, оконные функции, CTE) · [[Kafka]] · [[Redis]] · [[RabbitMQ]] · [[Celery]] · [[Чек-лист бэкенда]] (быстрые ответы на 30 секунд).
Разделы: [[#Транзакции и уровни изоляции]] · [[#Индексы]] · [[#EXPLAIN vs EXPLAIN ANALYZE]] · [[#Zero-downtime миграции]] · [[#JSON и JSONB]] · [[#Блокировки]] · [[#Эксплуатация]]

## Транзакции и уровни изоляции

**Суть.** Уровень изоляции — это контракт о том, какие аномалии конкурентного доступа БД допускает. Чем строже уровень, тем меньше аномалий, но больше откатов и ниже пропускная способность.

### Смежное: ACID и MVCC

- **Atomicity** — всё или ничего (в PG через WAL и статус транзакции в CLOG).
- **Consistency** — инварианты (constraints, FK) соблюдены до и после.
- **Isolation** — тема этого раздела.
- **Durability** — после COMMIT данные переживут падение (WAL + fsync; `synchronous_commit=off` жертвует этим ради скорости).

**MVCC в PG:** UPDATE не перезаписывает строку, а создаёт новую версию (tuple) с `xmin`/`xmax`. Каждая транзакция видит свой снимок. Отсюда «читатели не блокируют писателей» — и отсюда же мёртвые строки и необходимость VACUUM.

### Аномалии по уровням

| Уровень | Dirty read | Non-repeatable read | Phantom | Lost update | Write skew |
| --- | --- | --- | --- | --- | --- |
| Read Uncommitted | да (в PG нет) | да | да | да | да |
| Read Committed (PG default) | нет | да | да | да | да |
| Repeatable Read (PG = snapshot) | нет | нет | нет в PG | нет (ошибка сериализации) | да |
| Serializable (PG = SSI) | нет | нет | нет | нет | нет |

- **Dirty read** — читаем незакоммиченное.
- **Non-repeatable read** — повторный SELECT той же строки вернул другое значение.
- **Phantom** — повторный SELECT по условию вернул новые строки.
- **Lost update** — два read-modify-write, второй затёр первый.
- **Write skew** — две транзакции читают одно и пишут в РАЗНЫЕ строки, нарушая общий инвариант.

MySQL InnoDB: по умолчанию Repeatable Read, фантомы закрываются gap/next-key блокировками при locking reads; обычный SELECT читает снимок. Oracle — Read Committed и Serializable (по факту snapshot).

### Техника: lost update и 4 способа лечения

```sql
-- Плохо: два запроса параллельно прочитают 100 и оба запишут 90
SELECT balance FROM wallet WHERE id = 1;          -- 100
UPDATE wallet SET balance = 90 WHERE id = 1;      -- посчитано в приложении

-- 1. Атомарный UPDATE с условием (лучший вариант, если логика влезает)
UPDATE wallet SET balance = balance - 10
WHERE id = 1 AND balance >= 10
RETURNING balance;                                 -- 0 строк = недостаточно средств

-- 2. Пессимистичная блокировка
BEGIN;
SELECT balance FROM wallet WHERE id = 1 FOR UPDATE;
-- сложная логика в приложении
UPDATE wallet SET balance = 90 WHERE id = 1;
COMMIT;

-- 3. Оптимистичная блокировка (версия)
UPDATE wallet SET balance = 90, version = version + 1
WHERE id = 1 AND version = 7;                      -- 0 строк = конфликт, перечитать и повторить

-- 4. REPEATABLE READ / SERIALIZABLE + retry на SQLSTATE 40001
```

### Техника: write skew (классический пример с дежурством)

```sql
-- Инвариант: на дежурстве хотя бы 1 врач. Сейчас их 2.
-- T1 и T2 параллельно, обе на REPEATABLE READ:
SELECT count(*) FROM doctors WHERE on_call;        -- обе видят 2
UPDATE doctors SET on_call = false WHERE id = :me; -- разные строки → конфликта нет
COMMIT;                                            -- итог: 0 врачей
```

Лечение: `SERIALIZABLE` (одна из транзакций получит 40001), либо явно заблокировать общий ресурс (`SELECT ... FROM shifts WHERE id = :shift FOR UPDATE`), либо перенести инвариант в constraint.

### Техника: upsert и ON CONFLICT

```sql
-- Идемпотентная вставка: дубль просто не вставится
INSERT INTO processed_events (event_id) VALUES (:id) ON CONFLICT DO NOTHING;   -- 0 строк = дубль

-- Upsert: атомарно вставить или обновить
INSERT INTO cart_items (cart_id, sku, qty) VALUES (:c, :s, 1)
ON CONFLICT (cart_id, sku) DO UPDATE SET qty = cart_items.qty + EXCLUDED.qty
RETURNING qty;
```

- Гонку двух вставок решает уникальный индекс на колонках конфликта, а не `SELECT` перед `INSERT`: обе транзакции увидят «нет строки» и обе вставят.
- `DO NOTHING ... RETURNING` при конфликте не вернёт ничего — id существующей строки достают отдельным SELECT.
- Один ключ дважды в одном `INSERT ... ON CONFLICT DO UPDATE` — ошибка `cannot affect row a second time`: пачку дедуплицируют заранее.
- Значение sequence тратится и при конфликте — дыры в id нормальны.
- `MERGE` (PG 15+, `RETURNING` с PG 17) удобен для синхронизации по сложному условию, но на конкурентных вставках может упасть с unique violation; `ON CONFLICT` — нет.

### Техника: retry на serialization failure

```python
import asyncio
import random
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

RETRYABLE_SQLSTATES = {"40001", "40P01"}  # serialization failure, deadlock


async def run_serializable[T](
    session_factory: async_sessionmaker[AsyncSession],
    work: Callable[[AsyncSession], Awaitable[T]],
    *,
    max_attempts: int = 5,
) -> T:
    attempt = 1
    while True:
        try:
            async with session_factory() as session, session.begin():  # COMMIT на выходе — тоже внутри try
                await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
                return await work(session)
        except DBAPIError as exc:
            # sqlstate есть у ошибок и psycopg, и asyncpg (через адаптер SQLAlchemy)
            if attempt == max_attempts or getattr(exc.orig, "sqlstate", None) not in RETRYABLE_SQLSTATES:
                raise
        await asyncio.sleep(0.05 * 2**attempt * random.uniform(0.5, 1.5))  # backoff + jitter
        attempt += 1
```

> [!warning] Повторять всю транзакцию
> Повторять **всю** транзакцию целиком, включая чтения, и не делать внутри неё внешних побочных эффектов (HTTP, отправка в Kafka).

### Бизнес-кейсы

| Продуктовое требование | Какая аномалия угрожает | Решение |
| --- | --- | --- |
| «Баланс кошелька не уходит в минус» | lost update при параллельных списаниях | атомарный UPDATE с `balance >= x` + CHECK (balance >= 0); для финтеха — леджер из проводок вместо перезаписи баланса |
| «Нельзя забронировать слот дважды» | phantom / write skew (оба проверили «свободно» и вставили) | UNIQUE (room_id, slot) или `EXCLUDE USING gist (room_id WITH =, during WITH &&)` на пересечение диапазонов (для `=` по скаляру нужно расширение `btree_gist`) |
| «Промокод на первые 1000 покупателей» | гонка «проверил — записал»: lost update на счётчике или write skew, если считать строки использований | счётчик-строка с атомарным `UPDATE ... SET used = used + 1 WHERE used < 1000` |
| «Отчёт должен быть согласован по всем таблицам» | non-repeatable read между запросами отчёта | одна транзакция REPEATABLE READ READ ONLY (или на реплике) |
| «Перевод между счетами» | deadlock (A→B и B→A) | блокировать счета в порядке id |

### Что спросят дальше

- Почему бы не включить Serializable везде? — больше откатов под нагрузкой, нужен retry во всём коде, предикатные блокировки едят память, долгие транзакции конфликтуют чаще.
- Чем опасны долгие транзакции? — держат снимок, VACUUM не может удалить мёртвые строки, растёт bloat; `idle in transaction` держит блокировки. Защита: `idle_in_transaction_session_timeout`, `statement_timeout`.
- Как уровень изоляции связан с репликами? — чтение с асинхронной реплики может вернуть устаревшие данные независимо от уровня (read-your-writes ломается).

## Индексы

**Суть.** Индекс — отдельная структура, которая ускоряет поиск ценой записи и места. Индексы проектируют под запросы, а не под колонки.

### Типы индексов PostgreSQL

| Тип | Операции | Когда брать | Пример |
| --- | --- | --- | --- |
| B-tree | `=`, `<`, `>`, `BETWEEN`, `IN`, `ORDER BY`, `LIKE 'abc%'` (только при collation `C` или с `text_pattern_ops`), `IS NULL` | по умолчанию, ~90% случаев | `CREATE INDEX ON orders (user_id, created_at)` |
| Hash | только `=` | длинные ключи только на равенство; редко | `USING hash (session_token)` |
| GIN | `@>`, `?`, `&&`, `@@` | jsonb, массивы, full-text, триграммы | `USING gin (tags)` |
| GiST | `&&`, `<->`, `@>` для геометрии и диапазонов | гео, диапазоны, KNN, EXCLUDE | `USING gist (during)` |
| SP-GiST | несбалансированные деревья | IP-адреса, телефонные префиксы | `USING spgist (ip inet_ops)` |
| BRIN | диапазоны по блокам | гигантские append-only таблицы, корреляция с физ. порядком | `USING brin (created_at)` — килобайты вместо гигабайт |

### Техника: виды B-tree индексов

```sql
-- Составной: сначала равенство, потом диапазон/сортировка
CREATE INDEX idx_orders_user_created ON orders (user_id, created_at DESC);
-- работает:  WHERE user_id = 5 ORDER BY created_at DESC LIMIT 20
-- работает:  WHERE user_id = 5
-- НЕ работает эффективно: WHERE created_at > now() - '1 day' (нет левого префикса)

-- Частичный: индексируем только горячую часть
CREATE INDEX idx_orders_pending ON orders (created_at) WHERE status = 'pending';

-- Уникальность с условием (soft delete)
CREATE UNIQUE INDEX uq_users_email ON users (lower(email)) WHERE deleted_at IS NULL;

-- Покрывающий: index-only scan без хождения в таблицу
CREATE INDEX idx_orders_user_cover ON orders (user_id) INCLUDE (status, total);

-- По выражению: запрос должен содержать РОВНО это выражение
CREATE INDEX idx_users_email_lower ON users (lower(email));
-- WHERE lower(email) = 'a@b.c'  -- используется
-- WHERE email ILIKE 'a@b.c'     -- НЕТ

-- Поиск по подстроке
CREATE EXTENSION pg_trgm;
CREATE INDEX idx_products_name_trgm ON products USING gin (name gin_trgm_ops);
-- WHERE name ILIKE '%телефон%' — теперь по индексу
-- (для кириллицы нужна UTF-8 локаль базы: при LC_CTYPE=C pg_trgm не считает её буквами)

-- На проде всегда так (не блокирует запись, но нельзя в транзакции):
CREATE INDEX CONCURRENTLY ...;
-- если упал — останется INVALID индекс, его надо DROP и повторить
```

### Когда индекс НЕ используется

- Низкая селективность: запрос вернёт >5–10% таблицы — seq scan дешевле.
- Функция над колонкой: `WHERE date(created_at) = ...` → переписать на диапазон.
- Несовпадение типов: колонка `bigint`, а параметр пришёл `numeric` (`WHERE id = 42.0`, Decimal из приложения) — PG приводит колонку к `numeric`, и индекс по `bigint` не используется. (Пример с `phone text = 79001234567` — это MySQL, в PG такой запрос просто упадёт: оператора `text = bigint` нет.)
- `LIKE '%abc'` без триграммного индекса; `OR` по разным колонкам (иногда спасает BitmapOr или UNION).
- Устаревшая статистика — планировщик думает, что строк мало/много.
- Нет левого префикса составного индекса (в PG 18 появился skip scan, но он помогает только при малом числе значений первой колонки).

### Минусы индексов и почему нельзя индексировать всё

| Минус | Механизм | Чем проявляется |
| --- | --- | --- |
| Write amplification | каждая вставка пишет в таблицу + N индексов + WAL для каждого | падает TPS, растёт лаг репликации |
| Нет HOT-update | если меняется проиндексированная колонка, новая версия строки требует записей во ВСЕ индексы | индекс на `updated_at` или счётчике просмотров убивает дешёвые апдейты |
| Место и кэш | индексы конкурируют с данными за shared_buffers | суммарный размер индексов часто больше таблицы, падает cache hit |
| Bloat и VACUUM | мёртвые записи в каждом индексе | дольше вакуум, нужен REINDEX CONCURRENTLY |
| Планировщик | больше вариантов перебирать | рост planning time, иногда выбор худшего индекса |
| Создание | обычный CREATE INDEX берёт SHARE lock | запись в таблицу стоит на всё время построения |
| Бесполезность | 20 одиночных индексов не заменяют один правильный составной | индекс на boolean при 50/50 планировщик проигнорирует (при сильном перекосе нужен частичный индекс `WHERE is_flagged`) |

```sql
-- Найти неиспользуемые индексы (статистика с момента сброса, смотреть и на репликах!)
SELECT s.relname, s.indexrelname, s.idx_scan, pg_size_pretty(pg_relation_size(s.indexrelid))
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan = 0
  AND NOT i.indisunique          -- уникальные держат ограничение, даже если по ним не ищут
ORDER BY pg_relation_size(s.indexrelid) DESC;
```

### Бизнес-кейс: фильтры в каталоге

Продукт просит: «дайте пользователю фильтровать заказы по любой из 15 колонок и сортировать по любой». В лоб это 15+ индексов и всё равно медленные комбинации.

Как рассуждать:

1. Спросить, какие комбинации реально используются (аналитика, `pg_stat_statements`). Обычно 3–4 сценария дают 90% трафика.
2. Почти всегда есть обязательный фильтр (`tenant_id`, `user_id`) — он ставится первым во все составные.
3. Ограничить UX: обязательный период дат («не больше 90 дней»), курсорная пагинация вместо OFFSET.
4. Если нужен полноценный фасетный поиск — это сигнал вынести поиск в Elasticsearch/OpenSearch через CDC, а ПГ оставить источником правды.

```sql
-- Курсорная (keyset) пагинация вместо OFFSET 100000
SELECT * FROM orders
WHERE user_id = :u AND (created_at, id) < (:last_created_at, :last_id)
ORDER BY created_at DESC, id DESC
LIMIT 20;   -- индекс (user_id, created_at DESC, id DESC)
```

### Что спросят дальше

- Кластерный индекс в PG? — нет в смысле MySQL/SQL Server: таблица — куча (heap). `CLUSTER` упорядочивает один раз и берёт ACCESS EXCLUSIVE. В InnoDB данные лежат в PK, вторичные индексы ссылаются на PK — поэтому там важен короткий монотонный PK.
- UUID как PK? — UUIDv4 случайный: вставки в случайные места B-tree, page splits, плохая локальность кэша. Брать UUIDv7 (упорядочен по времени; в PG 18 есть `uuidv7()`).
- Как индекс сохраняет очередность с NULL? — B-tree хранит NULL; `NULLS FIRST/LAST` должно совпадать с ORDER BY.
- Селективность и кардинальность — в чём разница? Кардинальность — число уникальных значений; селективность — доля строк, которую отбирает конкретное условие.

## EXPLAIN vs EXPLAIN ANALYZE

**Суть.** `EXPLAIN` показывает план и *оценки* планировщика, запрос не выполняется. `EXPLAIN ANALYZE` *выполняет* запрос и добавляет фактические время, число строк и loops. Главная диагностика — сравнение `rows` оценки и `actual rows`.

### Техника: как запускать безопасно

```sql
-- Самый полезный вариант (с PG 18 BUFFERS включается при ANALYZE сам)
EXPLAIN (ANALYZE, BUFFERS, VERBOSE, SETTINGS) SELECT ...;

-- DML реально изменит данные — только так:
BEGIN;
EXPLAIN ANALYZE DELETE FROM sessions WHERE expires_at < now();
ROLLBACK;

-- На проде тяжёлый запрос можно не дождаться — сначала простой EXPLAIN,
-- либо ANALYZE на копии/реплике с SET statement_timeout
```

### Как читать план

```text
Limit  (cost=0.43..8.91 rows=20 width=64) (actual time=0.031..0.112 rows=20 loops=1)
  Buffers: shared hit=24
  ->  Index Scan using idx_orders_user_created on orders
        (cost=0.43..4210.11 rows=9932 width=64) (actual time=0.029..0.104 rows=20 loops=1)
        Index Cond: (user_id = 42)
Planning Time: 0.15 ms
Execution Time: 0.14 ms
```

- `cost=start..total` — условные единицы (seq_page_cost = 1). Стартовая стоимость важна для LIMIT.
- `actual time` и `rows` — **на один loop**; итого = значение × loops. Nested Loop с `loops=10000` и `time=0.5` — это 5 секунд.
- `Buffers: shared hit` — из кэша, `read` — с диска/из page cache ОС.
- `Rows Removed by Filter` — строки прочитаны зря: кандидат в индекс.
- `Heap Fetches` в Index Only Scan — видимость не всех страниц отмечена в visibility map, нужен VACUUM.
- `Sort Method: external merge Disk` — не хватило `work_mem`.

### Узлы плана

| Узел | Что делает | Когда хорош |
| --- | --- | --- |
| Seq Scan | читает всю таблицу | маленькая таблица или нужна большая доля строк |
| Index Scan | индекс → строка в heap по одной | мало строк, нужен порядок |
| Index Only Scan | всё из индекса | покрывающий индекс + свежий VACUUM |
| Bitmap Index + Heap Scan | собирает битмап страниц, читает последовательно | средняя селективность, комбинация индексов (BitmapAnd/Or) |
| Nested Loop | для каждой строки слева ищет справа | мало строк слева + индекс справа |
| Hash Join | строит хеш по меньшей стороне | большие наборы, равенство |
| Merge Join | сливает отсортированные | обе стороны уже отсортированы индексом |

### Типичные проблемы и лечение

- **Оценка rows=1, факт 100 000** → Nested Loop вместо Hash Join, запрос идёт минуты. Лечить: `ANALYZE table`, `ALTER TABLE ... ALTER COLUMN ... SET STATISTICS 1000`, `CREATE STATISTICS (dependencies) ON city, country FROM addresses` для коррелирующих колонок.
- **Generic plan у prepared statements** — после 5 выполнений PG может зафиксировать план, плохой для редких значений (`plan_cache_mode`).
- **Запрос быстрый в консоли, медленный в приложении** — другие параметры, generic plan, тип параметра из ORM (`bigint` против `numeric`, см. [[#Когда индекс НЕ используется]]).

### Смежное: как найти, что оптимизировать

```sql
-- Топ запросов по суммарному времени (а не по самому долгому!)
SELECT query, calls, round(total_exec_time) AS total_ms, round(mean_exec_time, 1) AS mean_ms
FROM pg_stat_statements
ORDER BY total_exec_time DESC LIMIT 10;
```

Также: `auto_explain` (логирует планы медленных запросов сам), `log_min_duration_statement`, визуализаторы explain.dalibo.com / explain.depesz.com.

### Бизнес-кейс

«После запуска акции личный кабинет тормозит». Один запрос на 10 мс с миллионом вызовов нагружает БД сильнее, чем отчёт на 10 секунд раз в день. Порядок: `pg_stat_statements` → топ по total time → EXPLAIN ANALYZE → индекс/переписывание/кэш. Часто причина — N+1 в ORM, а не сам запрос.

## Zero-downtime миграции

**Суть.** Пример — смена типа колонки на лету. `ALTER COLUMN TYPE` на большой таблице переписывает её целиком под `ACCESS EXCLUSIVE` — это минуты-часы полной недоступности. Решение — паттерн **expand → migrate → contract**: добавить новое рядом, перенести данные фоном, переключиться короткой блокировкой, удалить старое.

### Что дешево, а что переписывает таблицу

| Операция | Стоимость |
| --- | --- |
| `ADD COLUMN` nullable или с константным DEFAULT (PG 11+) | мгновенно, только каталог |
| `varchar(50) → varchar(100)`, `varchar → text` | мгновенно (бинарно совместимо) |
| `int → bigint`, `text → int` | **перезапись таблицы + всех индексов** |
| `timestamp → timestamptz` | перезапись; с PG 12 без неё, если `timezone = 'UTC'` в сессии |
| `SET NOT NULL` | полный скан под ACCESS EXCLUSIVE (если нет валидного CHECK) |
| `ADD FOREIGN KEY` / `ADD CHECK` | скан; с `NOT VALID` + `VALIDATE` — без долгой блокировки |
| `CREATE INDEX` | блокирует запись; `CONCURRENTLY` — нет |
| `DROP COLUMN` | мгновенно (колонка помечается удалённой), но под коротким ACCESS EXCLUSIVE |
| `ADD COLUMN ... GENERATED ALWAYS AS (...) STORED` | перезапись таблицы (с PG 18 есть `VIRTUAL` — без перезаписи, вычисляется при чтении) |

### Техника: `orders.id int → bigint` пошагово

```sql
-- Шаг 0. Защита от lock queue во всех DDL
SET lock_timeout = '3s';
SET statement_timeout = '30s';

-- Шаг 1. Expand: новая колонка (мгновенно)
ALTER TABLE orders ADD COLUMN id_new bigint;

-- Шаг 2. Двойная запись триггером — новые/изменённые строки сразу синхронны
CREATE FUNCTION orders_sync_id() RETURNS trigger AS $$
BEGIN NEW.id_new := NEW.id; RETURN NEW; END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_orders_sync BEFORE INSERT OR UPDATE ON orders
FOR EACH ROW EXECUTE FUNCTION orders_sync_id();

-- Шаг 3. Backfill батчами (скриптом в цикле, с паузами и контролем лага реплик)
UPDATE orders SET id_new = id
WHERE id BETWEEN :from AND :from + 10000 AND id_new IS NULL;
-- между батчами: проверить replay lag, дать отработать autovacuum

-- Шаг 4. Уникальный индекс под будущий PK — без блокировки записи
CREATE UNIQUE INDEX CONCURRENTLY orders_id_new_uq ON orders (id_new);

-- Шаг 5. NOT NULL без длинного скана под эксклюзивной блокировкой
ALTER TABLE orders ADD CONSTRAINT id_new_nn CHECK (id_new IS NOT NULL) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT id_new_nn;   -- скан, но под лёгкой блокировкой

-- Шаг 6. Переключение — одна короткая транзакция (с retry при lock_timeout)
BEGIN;
ALTER TABLE orders ALTER COLUMN id_new SET NOT NULL;  -- PG12+: пропустит скан благодаря CHECK
ALTER TABLE order_items DROP CONSTRAINT order_items_order_id_fkey;  -- FK на старый PK (их колонки уже bigint)
ALTER TABLE orders DROP CONSTRAINT orders_pkey;
ALTER TABLE orders ADD CONSTRAINT orders_pkey PRIMARY KEY USING INDEX orders_id_new_uq;
ALTER TABLE orders ALTER COLUMN id DROP DEFAULT,        -- иначе каждая вставка возьмёт два nextval,
                   ALTER COLUMN id DROP NOT NULL;      -- а старая int-колонка всё равно переполнится
ALTER SEQUENCE orders_id_seq AS bigint OWNED BY orders.id_new;  -- иначе sequence упрётся в лимит int
ALTER TABLE orders ALTER COLUMN id_new SET DEFAULT nextval('orders_id_seq');
ALTER TABLE orders RENAME COLUMN id TO id_old;
ALTER TABLE orders RENAME COLUMN id_new TO id;
DROP TRIGGER trg_orders_sync ON orders;
ALTER TABLE order_items ADD CONSTRAINT order_items_order_id_fkey
  FOREIGN KEY (order_id) REFERENCES orders (id) NOT VALID;  -- без скана
COMMIT;
ALTER TABLE order_items VALIDATE CONSTRAINT order_items_order_id_fkey;  -- скан под лёгкой блокировкой

-- Шаг 7. Contract: через релиз, когда убедились
ALTER TABLE orders DROP COLUMN id_old;
ALTER TABLE orders DROP CONSTRAINT id_new_nn;
```

Внешние ключи из других таблиц (`order_items.order_id`) переводят на bigint тем же путём **до** переключения. `DROP CONSTRAINT orders_pkey` без удаления FK упадёт, а `CASCADE` молча снесёт их все — поэтому FK удаляют и создают заново явно, `NOT VALID` + `VALIDATE`. Между COMMIT и VALIDATE новые строки уже проверяются, старые — ещё нет.

### Почему lock_timeout обязателен

Даже «мгновенный» ALTER должен получить ACCESS EXCLUSIVE. Если на таблице идёт долгий SELECT, ALTER ждёт его, а все новые SELECT встают в очередь **за ALTER**. Сервис лежит, хотя сама операция длится миллисекунды. С `lock_timeout` ALTER сдаётся и повторяется позже.

### Смежное: совместимость кода и схемы

- В каждый момент rolling-деплоя работают старая и новая версии приложения — схема должна подходить обеим.
- Переименование колонки = тот же expand/contract: добавить новую → писать в обе → читать из новой → убрать старую. Никогда `RENAME` одним релизом.
- Инструменты: Flyway/Liquibase (версионирование), pg_repack (перестройка без длинных блокировок), pgroll/reshape (автоматизация expand/contract), логическая репликация в новую таблицу/кластер.

### Бизнес-кейс: кончаются id

`int4` заканчивается на 2 147 483 647. При 10 млн вставок в день это ~214 дней; после все INSERT падают с `integer out of range` — продажи стоят. Решение надо начинать при ~50% заполнения: миграция идёт неделями. Аргумент для продукта: плановая задача на спринт сейчас против инцидента с недельным простоем потом. Мораль для новых таблиц: сразу `bigint` / `bigserial` / UUIDv7.

```sql
-- Мониторинг заполнения int-сиквенсов
SELECT sequencename, last_value, round(100.0 * last_value / 2147483647, 1) AS pct_used
FROM pg_sequences WHERE data_type = 'integer'::regtype ORDER BY pct_used DESC;
```

## JSON и JSONB

**Суть.** Почти всегда `jsonb`: бинарный, индексируемый, быстрый на чтение. Используется для полуструктурированных данных; то, по чему фильтруют, джойнят и что требует ограничений, живёт в обычных колонках.

|  | `json` | `jsonb` |
| --- | --- | --- |
| Хранение | текст как есть | разобранное бинарное дерево |
| Запись | быстрее (только валидация) | чуть медленнее (парсинг) |
| Чтение полей | парсинг при каждом обращении | быстро |
| Пробелы, порядок ключей, дубли | сохраняются | нет (последний дубль побеждает) |
| GIN-индекс | нет | да |
| Когда | нужна байт-в-байт копия (подпись вебхука, аудит) | всё остальное |

### Техника: операторы и индексы

```sql
CREATE TABLE products (
  id bigint PRIMARY KEY,
  category_id int NOT NULL,          -- фильтруем и джойним → колонка
  price numeric(12,2) NOT NULL,      -- сортируем, считаем → колонка
  attrs jsonb NOT NULL DEFAULT '{}'  -- характеристики, разные для категорий
);

SELECT attrs -> 'screen' ->> 'size' FROM products;           -- -> jsonb, ->> text
SELECT attrs #>> '{screen,size}' FROM products;               -- по пути
SELECT * FROM products WHERE attrs @> '{"color": "black"}';    -- containment
SELECT * FROM products WHERE attrs ? 'warranty';              -- есть ключ
SELECT * FROM products WHERE attrs @? '$.ram ? (@ >= 16)';    -- jsonpath
SELECT * FROM products WHERE (attrs ->> 'ram')::int >= 16;     -- с приведением

UPDATE products SET attrs = jsonb_set(attrs, '{color}', '"white"') WHERE id = 1;
UPDATE products SET attrs = attrs || '{"promo": true}' WHERE id = 1;   -- merge
UPDATE products SET attrs = attrs - 'promo' WHERE id = 1;              -- удалить ключ

-- Индексы
CREATE INDEX ON products USING gin (attrs);                 -- jsonb_ops: @>, ?, ?|, ?&, @?, @@
CREATE INDEX ON products USING gin (attrs jsonb_path_ops);  -- меньше и быстрее, но только @> и @?
CREATE INDEX ON products ((attrs ->> 'brand'));             -- B-tree на одно поле: =, ORDER BY

-- Валидация структуры
ALTER TABLE products ADD CONSTRAINT attrs_is_object CHECK (jsonb_typeof(attrs) = 'object');

-- Промоутим горячее поле в настоящую колонку
-- STORED переписывает таблицу под ACCESS EXCLUSIVE — на большой таблице через expand/contract
ALTER TABLE products ADD COLUMN brand text GENERATED ALWAYS AS (attrs ->> 'brand') STORED;
```

> [!warning] Ловушка
> GIN-индекс по `attrs` **не** ускорит `attrs ->> 'brand' = 'Apple'` — нужен либо `@> '{"brand":"Apple"}'`, либо индекс по выражению.

### Когда jsonb — да, а когда нет

| Хорошо подходит | Плохо подходит |
| --- | --- |
| атрибуты, разные для разных типов сущностей (каталог) | поля для JOIN, FK, агрегатов и сортировки |
| сырые payload’ы внешних API, вебхуки | денежные суммы, статусы, всё, что требует NOT NULL/CHECK |
| настройки пользователя, feature flags | частые точечные апдейты большого документа (переписывается целиком, TOAST > 2 КБ) |
| история/аудит изменений, события, outbox payload | «схема ещё не понятна» как повод не проектировать схему |
| денормализованный снимок (состав заказа на момент покупки) | сложная аналитика: у планировщика нет статистики по ключам (оценка ~0.1%) |

### Бизнес-кейсы

- **Маркетплейс с сотнями категорий.** У телефона — диагональ и RAM, у платья — размер и ткань. Альтернативы: EAV (таблица entity-attribute-value — много JOIN, больно), таблица на категорию (миграция на каждую новую категорию), jsonb (гибко, но валидация в приложении). Обычный выбор — jsonb + схема атрибутов категории в справочнике + фасетный поиск в Elasticsearch.
- **Вебхуки платёжного провайдера.** Сырое тело храним как есть (разбор споров, повторная обработка), а `payment_id`, `status`, `amount` вытаскиваем в колонки — по ним ищут и сверяют.
- **«Давайте хранить весь заказ одним JSON, как в Mongo».** Продукт быстрее стартует, но через полгода финансы просят отчёт «выручка по SKU за квартал», и он становится дорогим и ненадёжным. Компромисс: строки заказа — таблицей, снимок карточки товара на момент покупки — jsonb.

### Что спросят дальше

- Что такое TOAST? — значения > ~2 КБ сжимаются и выносятся в отдельную таблицу; чтение одного поля большого jsonb детостит весь документ.
- PG или MongoDB? — PG, если нужны транзакции между сущностями, JOIN и строгие связи; Mongo — если документ самодостаточен и нужен встроенный шардинг.

## Блокировки

**Суть.** В PG благодаря MVCC читатели и писатели не блокируют друг друга. Конфликтуют запись с записью одной строки и DDL со всем остальным. Блокировки бывают пессимистичные (захватываем заранее) и оптимистичные (проверяем конфликт при записи).

### Общие понятия (для любой БД)

- **Shared (S)** — много читателей; **Exclusive (X)** — один писатель.
- **Гранулярность**: строка / страница / таблица. SQL Server умеет lock escalation (много строчных → табличная), PG — нет: строчные блокировки хранятся прямо в строке (xmax).
- **Intent locks** (IS/IX) — пометка на таблице «внутри кто-то держит строки», чтобы не проверять каждую строку.
- **2PL (two-phase locking)** — сначала только захват, потом только освобождение; даёт сериализуемость ценой блокировок читателей.
- **Gap / next-key locks** (InnoDB) — блокируют «промежутки» индекса, чтобы нельзя было вставить фантом.
- **Распределённые блокировки** — Redis (Redlock, с оговорками, см. [[Redis#Техника: распределённая блокировка]]), ZooKeeper/etcd, либо advisory locks в PG. Всегда с TTL и fencing token.

### Строчные блокировки PG

| Режим | Кто берёт | Блокирует |
| --- | --- | --- |
| `FOR UPDATE` | DELETE, UPDATE ключевых полей, явный SELECT | все другие строчные режимы |
| `FOR NO KEY UPDATE` | обычный UPDATE | всё, кроме KEY SHARE (FK-проверки не ждут) |
| `FOR SHARE` | явный SELECT | изменения строки |
| `FOR KEY SHARE` | проверка FK при вставке в дочернюю таблицу | DELETE и смену ключа |

Модификаторы: `NOWAIT` — сразу ошибка; `SKIP LOCKED` — пропустить занятые строки.

### Табличные блокировки PG (основные)

| Режим | Кто берёт | Конфликтует с |
| --- | --- | --- |
| ACCESS SHARE | SELECT | только ACCESS EXCLUSIVE |
| ROW EXCLUSIVE | INSERT/UPDATE/DELETE | SHARE и сильнее |
| SHARE UPDATE EXCLUSIVE | VACUUM, ANALYZE, CREATE INDEX CONCURRENTLY, VALIDATE CONSTRAINT | сам с собой и сильнее |
| SHARE | CREATE INDEX | любая запись |
| ACCESS EXCLUSIVE | ALTER TABLE (большинство), DROP, TRUNCATE, VACUUM FULL | всё, включая SELECT |

### Техника: очередь задач на SKIP LOCKED

```sql
-- Несколько воркеров берут разные задачи без конкуренции и дублей
WITH next AS (
  SELECT id FROM jobs
  WHERE status = 'new' AND run_at <= now()
  ORDER BY run_at
  LIMIT 10
  FOR UPDATE SKIP LOCKED
)
UPDATE jobs SET status = 'processing', locked_at = now(), attempts = attempts + 1
FROM next WHERE jobs.id = next.id
RETURNING jobs.*;
-- индекс: CREATE INDEX ON jobs (run_at) WHERE status = 'new';
```

Так устроены outbox-релеи ([[Kafka#Смежное: transactional outbox]]), pg-boss, graphile-worker, River, procrastinate. Порядок `SKIP LOCKED` не гарантирует: два воркера берут соседние пачки. Нужен «сборщик» зависших задач: `status = 'processing' AND locked_at < now() - interval '5 min'` → вернуть в `new`.

### Техника: advisory locks и deadlock

```sql
-- Только один инстанс выполняет ночной расчёт (без таблицы-флага)
SELECT pg_try_advisory_lock(hashtext('nightly_billing'));   -- true = мы главные
-- ... работа ...
SELECT pg_advisory_unlock(hashtext('nightly_billing'));
-- pg_advisory_xact_lock — сама отпустится по концу транзакции
-- Осторожно: сессионные advisory locks ломаются с PgBouncer в transaction mode

-- Deadlock: T1 блокирует A, потом B; T2 — B, потом A.
-- PG через deadlock_timeout (1s) находит цикл и убивает одну (SQLSTATE 40P01).
-- Профилактика — единый порядок захвата:
SELECT * FROM accounts WHERE id IN (:a, :b) ORDER BY id FOR UPDATE;

-- Кто кого блокирует
SELECT pid, pg_blocking_pids(pid) AS blocked_by, wait_event_type, state, left(query, 80)
FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;
```

### Пессимистичная vs оптимистичная

|  | Пессимистичная (`FOR UPDATE`) | Оптимистичная (`version`) |
| --- | --- | --- |
| Когда | высокая конкуренция за одну строку, короткая транзакция | конфликты редки, долгое «размышление» (форма в UI) |
| Цена | ожидания, риск deadlock, держит соединение | retry или ошибка пользователю |
| Пример | списание с баланса | редактирование карточки товара двумя менеджерами |

### Бизнес-кейс: флеш-распродажа

«100 телефонов по акции, 50 000 человек жмут кнопку одновременно». Все транзакции бьются за одну строку `stock` (hot row) — очередь блокировок, исчерпание пула соединений, таймауты всего сайта.

Варианты решения, от простого к сложному:

1. Атомарный `UPDATE stock SET qty = qty - 1 WHERE sku = :s AND qty > 0` — короткая блокировка, без чтения в приложение.
2. Разбить остаток на N строк-«бакетов» (10 по 10 шт.) и списывать из случайного.
3. Предварительно создать 100 строк-«талонов» и раздавать через `FOR UPDATE SKIP LOCKED LIMIT 1`.
4. Вынести счётчик в [[Redis]] (`DECR`), в БД писать уже выигравших; перед всем этим — виртуальная очередь на фронте.

Продуктовый вопрос, который надо задать: нужен ли строгий лимит или допустим небольшой оверсейл с последующей отменой и извинением? Ответ меняет архитектуру.

### Что спросят дальше

- Что такое lock queue и почему «быстрый» ALTER положил прод? — см. [[#Почему lock_timeout обязателен]].
- Блокирует ли SELECT обычный UPDATE в PG? — нет. А VACUUM FULL? — да, и совсем.
- Почему вставка в дочернюю таблицу ждёт родителя? — FK берёт `FOR KEY SHARE` на строку родителя; конфликт с DELETE или изменением его PK.

## Эксплуатация

Темы, которые всплывают рядом с любым вопросом про PostgreSQL на проде.

### VACUUM и bloat

MVCC оставляет мёртвые версии строк. `VACUUM` помечает их место свободным (файл не уменьшается, кроме пустого хвоста), обновляет visibility map и замораживает старые xid. `VACUUM FULL` переписывает таблицу под ACCESS EXCLUSIVE — на проде вместо него pg_repack.

- **Враги вакуума**: долгие транзакции, `idle in transaction`, забытые слоты репликации, `hot_standby_feedback` с долгими запросами на реплике.
- **Transaction ID wraparound**: xid 32-битный; если не замораживать, PG перестанет принимать запись. Мониторинг: `age(datfrozenxid)`.
- Для горячих таблиц автовакуум тюнят точечно: `ALTER TABLE jobs SET (autovacuum_vacuum_scale_factor = 0.01)`.

### Репликация

| Вид | Как работает | Зачем |
| --- | --- | --- |
| Физическая (streaming) | копирует WAL побайтно, реплика целиком read-only | HA, чтение с реплик |
| Логическая | публикации/подписки на уровне строк | миграции между версиями, CDC, частичная репликация |
| Синхронная | COMMIT ждёт подтверждения реплики | RPO = 0 ценой latency |
| Асинхронная | не ждёт | быстро, но лаг и возможная потеря при failover |

Бизнес-кейс: «пользователь сохранил профиль, обновил страницу — а там старые данные». Чтение с реплики ломает read-your-writes. Решения: читать с праймари N секунд после записи этого пользователя, или сравнивать LSN.

### Партиционирование и шардирование

```sql
CREATE TABLE events (id bigint, created_at timestamptz NOT NULL, payload jsonb)
PARTITION BY RANGE (created_at);
CREATE TABLE events_2026_11 PARTITION OF events
  FOR VALUES FROM ('2026-11-01 00:00+00') TO ('2026-12-01 00:00+00');  -- зона явно: иначе границы в TimeZone сессии
-- Удаление старых данных = DETACH/DROP партиции вместо массового DELETE
```

- Польза: partition pruning, дешёвая очистка по retention, маленькие индексы.
- Подводные камни: запросы без ключа партиционирования идут по всем партициям; PK/UNIQUE обязаны включать ключ партиционирования; тысячи партиций тормозят планировщик.
- Шардирование (данные на разных серверах: Citus, прикладное шардирование по tenant_id) — когда упёрлись в запись одного сервера. Цена: кросс-шардовые транзакции и JOIN.

### Пулы соединений

Соединение PG = процесс (~5–10 МБ). 50 подов × пул 20 = 1000 соединений — БД захлебнётся на context switching. Решение: PgBouncer в transaction mode (но тогда нельзя сессионные фичи: `SET` (а `SET LOCAL` внутри транзакции можно), сессионные advisory locks, LISTEN; prepared statements — только с PgBouncer 1.21+ и `max_prepared_statements`). asyncpg по умолчанию кэширует prepared statements — за PgBouncer старой версии ему ставят `statement_cache_size=0`. Ориентир для активных соединений — несколько на ядро CPU.

### Бэкапы и PITR

- `pg_dump` — логическая копия одной базы: переносима между версиями, но большая база восстанавливается часами, и данные — только на момент дампа.
- Физический бэкап (`pg_basebackup`, pgBackRest, WAL-G) + архив WAL → **PITR**: восстановление на любой момент, например за минуту до `DELETE` без `WHERE`.
- **RPO** — сколько данных можно потерять (частота архивации WAL, синхронная реплика), **RTO** — за сколько поднимемся (размер базы, скорость восстановления, автоматический failover через Patroni).
- Реплика — не бэкап: `DROP TABLE` мгновенно уедет и на неё. Бэкап, который ни разу не восстанавливали, — не бэкап.
