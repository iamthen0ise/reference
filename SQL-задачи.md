---
title: SQL-задачи
---

Разбор типовых SQL-задач с live-coding собеседований бэкенд-разработчика. Диалект — PostgreSQL 15+, все запросы прогнаны на PostgreSQL 17 на одной общей схеме из первого раздела. В каждой задаче: условие, решение, почему так, типичная ошибка, альтернатива и её цена, нужный индекс.
Рядом: [[PostgreSQL]] (индексы, планы, блокировки) · [[Чек-лист бэкенда]] · [[Паттерны задач]] (алгоритмические задачи)
Разделы: [[#Схема для примеров]] · [[#Порядок выполнения запроса]] · [[#JOIN и подзапросы]] · [[#Агрегация]] · [[#Оконные функции]] · [[#Классические задачи]] · [[#CTE и LATERAL]] · [[#Ловушки]]

> [!tip] Как выбрать приём
> Читай условие и ищи ключевую фразу.
> - «топ-N в каждой группе» → `ROW_NUMBER` / `DENSE_RANK` ([[#Топ-N в каждом отделе]]) или `LATERAL` с `LIMIT`, если есть индекс и мало групп с большим хвостом ([[#LATERAL как топ-N в группе]])
> - «N-я по величине» → `DENSE_RANK` или `OFFSET` ([[#N-я по величине зарплата]])
> - «N дней подряд», «серии», «непрерывные периоды» → gaps and islands ([[#Дни подряд]])
> - «есть в A, нет в B» → `NOT EXISTS` ([[#Клиенты без заказов]])
> - «есть в A и в B, но без дублей» → `EXISTS`, а не `JOIN` + `DISTINCT`
> - «по месяцам, включая пустые» → `generate_series` + `LEFT JOIN` ([[#Выручка по месяцам с пустыми месяцами]])
> - «иерархия любой глубины», «все подчинённые» → рекурсивный CTE ([[#Все подчинённые любой глубины]])
> - «рост к прошлому периоду», «разница с предыдущей строкой» → `LAG` / `LEAD`
> - «нарастающий итог», «скользящее среднее» → агрегат с `OVER (ORDER BY ...)` и рамкой
> - «доля от итога» → `sum() OVER ()`
> - «последняя/первая запись по ключу» → `DISTINCT ON` или `ROW_NUMBER`
> - «медиана», «перцентиль» → `percentile_cont`
> - «пропуски в id» → `LEAD` или `generate_series` + `LEFT JOIN`
> - «удалить дубли» → `ROW_NUMBER` по ключу дубля, удалять `rn > 1`

## Схема для примеров

Все задачи ниже — на этой схеме. Данные подобраны так, чтобы ловушки проявлялись: `NULL` в `manager_id` и `amount`, ничья в зарплатах, пропуск дня в `logins`, пустой март в `orders`, клиент без заказов, отдел без сотрудников, два отдела с одинаковой средней зарплатой.

```sql
CREATE TABLE departments (
    id   int PRIMARY KEY,
    name text NOT NULL
);

CREATE TABLE employees (
    id            int PRIMARY KEY,
    name          text NOT NULL,
    department_id int REFERENCES departments (id),
    manager_id    int REFERENCES employees (id),   -- NULL у топ-менеджера
    salary        int NOT NULL
);

CREATE TABLE customers (
    id   int PRIMARY KEY,
    name text NOT NULL
);

CREATE TABLE orders (
    id          int PRIMARY KEY,
    customer_id int NOT NULL REFERENCES customers (id),
    amount      numeric(10, 2),                    -- может быть NULL
    status      text NOT NULL,                     -- new / paid / cancelled
    created_at  timestamptz NOT NULL
);

CREATE TABLE logins (
    user_id int  NOT NULL REFERENCES customers (id),
    day     date NOT NULL,
    PRIMARY KEY (user_id, day)
);

-- вспомогательные таблицы для двух задач
CREATE TABLE emails  (id serial PRIMARY KEY, email text NOT NULL);
CREATE TABLE tickets (id int PRIMARY KEY);

INSERT INTO departments VALUES (1, 'Backend'), (2, 'Data'), (3, 'Ops'), (4, 'Design');

INSERT INTO employees VALUES
    (1, 'Anna',  1, NULL, 300),
    (2, 'Boris', 1, 1,    200),
    (3, 'Vera',  1, 1,    200),   -- ничья с Boris
    (4, 'Gleb',  1, 2,    150),
    (5, 'Dina',  2, 1,    250),
    (6, 'Egor',  2, 5,    260),   -- получает больше своего менеджера
    (7, 'Zoya',  2, 5,    100),
    (8, 'Kira',  3, NULL, 225),
    (9, 'Ivan',  3, 8,    200);

INSERT INTO customers VALUES (1, 'Ann'), (2, 'Bob'), (3, 'Cat'), (4, 'Dan');   -- у Cat нет заказов

INSERT INTO orders VALUES
    (1, 1, 100,  'paid',      '2025-01-05 10:00+00'),
    (2, 1, 50,   'paid',      '2025-01-20 09:00+00'),
    (3, 2, 200,  'paid',      '2025-01-31 23:30+00'),   -- конец января
    (4, 1, 70,   'cancelled', '2025-02-03 11:00+00'),
    (5, 2, 120,  'paid',      '2025-02-14 15:00+00'),
    (6, 4, 300,  'paid',      '2025-02-28 12:00+00'),
    (7, 2, 80,   'new',       '2025-04-02 08:00+00'),   -- в марте заказов нет
    (8, 4, 150,  'paid',      '2025-04-15 13:00+00'),
    (9, 4, NULL, 'new',       '2025-04-20 14:00+00');

INSERT INTO logins VALUES
    (1, '2025-03-01'), (1, '2025-03-02'), (1, '2025-03-03'),
    (1, '2025-03-05'), (1, '2025-03-06'),                      -- 4 марта пропущено
    (2, '2025-03-01'), (2, '2025-03-02'),
    (4, '2025-03-10'), (4, '2025-03-11'), (4, '2025-03-12'), (4, '2025-03-13');

INSERT INTO emails (email) VALUES
    ('a@x.io'), ('b@x.io'), ('a@x.io'), ('c@x.io'), ('b@x.io'), ('a@x.io');
INSERT INTO tickets VALUES (1), (2), (3), (6), (7), (10);
```

Сессия в UTC: `date_trunc` по `timestamptz` считает границы месяца в часовом поясе сессии. В проде задавай пояс явно (`AT TIME ZONE`).

## Порядок выполнения запроса

Логический порядок не совпадает с написанным:

1. `FROM` / `JOIN` — собрать строки
2. `WHERE` — отфильтровать строки (до группировки)
3. `GROUP BY` — сгруппировать
4. `HAVING` — отфильтровать группы
5. оконные функции — считаются по строкам, прошедшим шаги 1–4
6. `SELECT` — вычислить выражения и алиасы
7. `DISTINCT`
8. `ORDER BY`
9. `LIMIT` / `OFFSET`

Из этого следуют два вопроса, которые задают на каждом собеседовании.

### Алиас из SELECT в WHERE

`WHERE` выполняется раньше `SELECT`, алиаса ещё нет.

```sql
SELECT salary * 12 AS yearly FROM employees WHERE yearly > 3000;
-- ERROR: column "yearly" does not exist
```

Повтори выражение в `WHERE` или вынеси в подзапрос / CTE. Алиас работает в `ORDER BY`, а в PostgreSQL ещё и в `GROUP BY` (`GROUP BY d` по алиасу `department_id AS d`), но не в `HAVING`.

### Оконная функция в WHERE

Окна считаются после `WHERE` и `HAVING`, поэтому условие по ним в `WHERE` запрещено.

```sql
SELECT name FROM employees WHERE row_number() OVER (ORDER BY salary) = 1;
-- ERROR: window functions are not allowed in WHERE
```

Решение — подзапрос или CTE: посчитать окно внутри, отфильтровать снаружи.

```sql
SELECT name, salary FROM (
    SELECT name, salary, row_number() OVER (ORDER BY salary DESC) AS rn
    FROM employees
) t
WHERE rn <= 3;   -- Anna, Egor, Dina
```

> [!tip] Следствие
> Фильтр в `WHERE` уменьшает вход окна и агрегата, фильтр снаружи окна — нет. `WHERE status = 'paid'` перед `sum() OVER ()` изменит итог, внешний фильтр после окна — не изменит.

## JOIN и подзапросы

`INNER` оставляет пары с совпадением; `LEFT` — все строки левой таблицы, справа `NULL`, если пары нет; `FULL` — все строки обеих; `CROSS` — декартово произведение. `RIGHT` почти не нужен: меняй таблицы местами.

### Условие на правую таблицу в WHERE

```sql
-- Ловушка: Cat исчезла, LEFT JOIN стал INNER
SELECT c.name, o.id
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id
WHERE o.status = 'paid';
```

Для Cat `o.status` — `NULL`, `NULL = 'paid'` не истина, строка отфильтрована. Условие на правую таблицу кладут в `ON`:

```sql
SELECT c.name, count(o.id) AS paid_orders
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id AND o.status = 'paid'
GROUP BY c.id, c.name
ORDER BY c.id;   -- Ann 2, Bob 2, Cat 0, Dan 2
```

Для `INNER JOIN` разницы между `ON` и `WHERE` нет, для `LEFT` — принципиальная.

### Клиенты без заказов

Условие: клиенты, у которых нет ни одного заказа (anti-join).

```sql
SELECT c.id, c.name
FROM customers c
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id);   -- Cat
```

Альтернатива — `LEFT JOIN ... WHERE o.id IS NULL`. Планировщик строит из обоих один Anti Join, результат тот же; `NOT EXISTS` читается как условие, а не как трюк с `NULL`.

> [!warning] NOT IN и NULL
> `x NOT IN (подзапрос)` возвращает пусто, если в подзапросе есть хоть один `NULL`: `x <> NULL` даёт неизвестность, и условие для всех строк не истинно.
> ```sql
> -- сотрудники, которые не являются менеджерами; у Anna manager_id IS NULL
> SELECT name FROM employees WHERE id NOT IN (SELECT manager_id FROM employees);   -- 0 строк
>
> SELECT name FROM employees e
> WHERE NOT EXISTS (SELECT 1 FROM employees s WHERE s.manager_id = e.id);          -- Vera, Gleb, Egor, Zoya, Ivan
> ```
> Для клиентов без заказов `NOT IN` случайно работает, пока `orders.customer_id NOT NULL`. Это хрупко: через год колонку сделают nullable, и запрос молча вернёт пусто. Всегда `NOT EXISTS`.

Индекс: `orders (customer_id)`. Без него `NOT EXISTS` для каждого клиента сканирует `orders`.

### Semi-join: клиенты с оплаченным заказом

```sql
SELECT c.name
FROM customers c
WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.status = 'paid');
-- Ann, Bob, Dan по одному разу
```

`JOIN orders` вернул бы Ann дважды (по числу заказов). `EXISTS` останавливается на первом совпадении и не размножает строки. `DISTINCT` после `JOIN` даёт тот же ответ, но сначала строит все пары, потом дедуплицирует.

### Сотрудники, получающие больше своего менеджера

Self-join: таблица присоединяется к самой себе по `manager_id`.

```sql
SELECT e.name, e.salary, m.name AS manager, m.salary AS manager_salary
FROM employees e
JOIN employees m ON m.id = e.manager_id
WHERE e.salary > m.salary;   -- Egor 260 > Dina 250
```

`INNER JOIN` сам отбрасывает топ-менеджеров без `manager_id`. Если в условии «вывести всех сотрудников и имя менеджера (если есть)» — нужен `LEFT JOIN`.

### Размножение строк при JOIN двух «один-ко-многим»

Условие: суммарная выручка и число дней активности по клиентам. И у `orders`, и у `logins` связь с клиентом один-ко-многим.

```sql
-- Ловушка: Ann получает 1100 вместо 220
SELECT c.name, sum(o.amount) AS total, count(l.day) AS days
FROM customers c
JOIN orders o ON o.customer_id = c.id
JOIN logins l ON l.user_id = c.id
GROUP BY c.id, c.name ORDER BY c.id;
-- Ann 1100.00 / 15, Bob 800.00 / 6, Dan 1800.00 / 12
```

У Ann 3 заказа и 5 входов: `JOIN` даёт 15 строк, каждый заказ повторён 5 раз. Решение — агрегировать каждую сторону до `JOIN`, чтобы соединялись по одной строке на клиента:

```sql
SELECT c.name, o.total, l.days
FROM customers c
JOIN (SELECT customer_id, sum(amount) AS total FROM orders GROUP BY customer_id) o ON o.customer_id = c.id
JOIN (SELECT user_id, count(*) AS days FROM logins GROUP BY user_id) l ON l.user_id = c.id
ORDER BY c.id;
-- Ann 220.00 / 5, Bob 400.00 / 2, Dan 450.00 / 4
```

Альтернатива — коррелированные подзапросы в `SELECT`: проще писать, но считаются по строке внешнего запроса. Признак бага: итоги кратно больше ожидаемых.

## Агрегация

### GROUP BY и HAVING

`WHERE` фильтрует строки до группировки, `HAVING` — группы после. Условие по агрегату (`count(*) > 2`) в `WHERE` писать нельзя.

```sql
SELECT customer_id, count(*) AS n, sum(amount) AS s
FROM orders
GROUP BY customer_id
HAVING count(*) > 2;   -- клиенты 1, 2, 4
```

Всё, что есть в `SELECT` и не внутри агрегата, должно быть в `GROUP BY`. Исключение: колонки, функционально зависимые от первичного ключа, указанного в `GROUP BY` (`GROUP BY c.id` разрешает `c.name`). Я всё равно перечисляю обе: так переносимее.

### COUNT(*), COUNT(col), COUNT(DISTINCT col)

```sql
SELECT count(*) AS all_rows,                       -- 9: все строки
       count(amount) AS non_null,                  -- 8: только где amount IS NOT NULL
       count(DISTINCT customer_id) AS customers    -- 3: уникальные ненулевые
FROM orders;
```

Ловушка: `count(col)` пропускает `NULL`. После `LEFT JOIN` считай `count(o.id)`, а не `count(*)`: для клиента без заказов `count(*)` вернёт 1 (строка с `NULL`), а `count(o.id)` — 0.

### Условная агрегация и FILTER

Условие: по клиенту число заказов по статусам и сумма оплаченных (pivot по статусам).

```sql
SELECT customer_id,
       count(*) FILTER (WHERE status = 'paid')      AS paid,
       count(*) FILTER (WHERE status = 'new')       AS new,
       count(*) FILTER (WHERE status = 'cancelled') AS cancelled,
       sum(amount) FILTER (WHERE status = 'paid')   AS paid_sum
FROM orders
GROUP BY customer_id
ORDER BY customer_id;
-- 1: 2/0/1/150.00   2: 2/1/0/320.00   4: 2/1/0/450.00
```

Один проход по таблице вместо трёх `GROUP BY` с `JOIN`. Классический вариант без `FILTER` — `sum(CASE WHEN status = 'paid' THEN amount END)`: тот же результат, работает в любом диалекте, но шумнее. Для `count` через `CASE` пиши `THEN 1` без `ELSE 0`: иначе `count` посчитает и нули.

### NULL в агрегатах

- `sum`, `avg`, `min`, `max` игнорируют `NULL`.
- `sum` по пустому набору — `NULL`, а не 0.
- `avg(col)` делит на число ненулевых, а не на число строк.

```sql
SELECT sum(amount) FROM orders WHERE status = 'refunded';                 -- NULL
SELECT coalesce(sum(amount), 0) FROM orders WHERE status = 'refunded';    -- 0

-- у Dan три заказа, один с amount NULL
SELECT avg(amount)               AS avg_skip_null,   -- 225: (300 + 150) / 2
       sum(amount) / count(*)    AS sum_over_all,    -- 150: (300 + 150) / 3
       avg(coalesce(amount, 0))  AS avg_zero         -- 150
FROM orders WHERE customer_id = 4;
```

Что такое «средний заказ» при `NULL` — вопрос к бизнесу, решение должно быть осознанным. `coalesce` ставь снаружи агрегата, если нужен результат для пустой группы; внутри — если `NULL` по смыслу ноль.

### Доля и целочисленное деление

Условие: процент оплаченных заказов.

```sql
SELECT count(*) FILTER (WHERE status = 'paid') / count(*)                     AS bad,    -- 0
       round(100.0 * count(*) FILTER (WHERE status = 'paid') / count(*), 1)   AS pct     -- 66.7
FROM orders;
```

`6 / 9` для `bigint` — целочисленное деление, ноль. Домножь на `100.0` или приведи `::numeric` до деления; порядок важен: `count(*) FILTER (...) / count(*) * 100.0` даст 0, деление уже целочисленное.

## Оконные функции

Окно считает значение для каждой строки, не схлопывая их. `PARTITION BY` делит на группы, `ORDER BY` задаёт порядок внутри, рамка (`ROWS` / `RANGE` / `GROUPS`) — какие строки видит функция.

### ROW_NUMBER, RANK, DENSE_RANK

Три способа ранжировать, различаются на ничьей. Отдел 1, ничья Boris и Vera:

```sql
SELECT name, salary,
       row_number() OVER w AS rn,
       rank()       OVER w AS rnk,
       dense_rank() OVER w AS drnk
FROM employees
WHERE department_id = 1
WINDOW w AS (ORDER BY salary DESC)
ORDER BY salary DESC, id;
```

| name | salary | rn | rnk | drnk |
| --- | --- | --- | --- | --- |
| Anna | 300 | 1 | 1 | 1 |
| Boris | 200 | 2 | 2 | 2 |
| Vera | 200 | 3 | 2 | 2 |
| Gleb | 150 | 4 | 4 | 3 |

- `ROW_NUMBER` — уникальный номер, на ничьей порядок произвольный (добавь tie-breaker в `ORDER BY`).
- `RANK` — одинаковый ранг на ничьей, следующий с пропуском.
- `DENSE_RANK` — без пропусков.

Выбор: нужно ровно N строк — `ROW_NUMBER`; нужны все равные границе — `RANK` или `DENSE_RANK`.

### Топ-N в каждом отделе

Условие: двое самых высокооплачиваемых в каждом отделе.

```sql
SELECT department_id, name, salary
FROM (
    SELECT department_id, name, salary,
           dense_rank() OVER (PARTITION BY department_id ORDER BY salary DESC) AS dr
    FROM employees
) t
WHERE dr <= 2
ORDER BY department_id, salary DESC, name;
```

В отделе 1 вернутся трое: Anna и ничья Boris и Vera. С `ROW_NUMBER` Vera потерялась бы случайно, поэтому уточни у интервьюера, что делать с ничьей. Индекс: `employees (department_id, salary DESC)` позволяет читать группу уже отсортированной, но окно всё равно считается по всей таблице. Для «топ-N из очень многих строк в немногих группах» лучше `LATERAL` ([[#LATERAL как топ-N в группе]]).

### N-я по величине зарплата

Условие: вторая по величине зарплата; если такой нет — `NULL`.

```sql
-- DENSE_RANK: ничья не сдвигает номер
SELECT salary
FROM (SELECT salary, dense_rank() OVER (ORDER BY salary DESC) AS dr FROM employees) t
WHERE dr = 2
GROUP BY salary;                                           -- 260

-- OFFSET: скалярный подзапрос сам превращает «нет строки» в NULL
SELECT (SELECT DISTINCT salary FROM employees
        ORDER BY salary DESC OFFSET 1 LIMIT 1) AS second_salary;       -- 260
SELECT (SELECT DISTINCT salary FROM employees
        ORDER BY salary DESC OFFSET 99 LIMIT 1) AS salary_99;          -- NULL
```

Окно при `dr = 99` вернёт 0 строк, а не `NULL`; если нужен именно `NULL` — оберни во внешний `SELECT (...)`. Для второго максимума годится `max(salary) WHERE salary < (SELECT max(salary) ...)`, но для N > 2 не масштабируется. Типичная ошибка: `OFFSET 1` без `DISTINCT` вернёт вторую строку, а не второе значение: при дубле на первом месте это будет то же число, что на первом.

### Нарастающий итог

```sql
SELECT id, created_at::date AS day, amount,
       sum(amount) OVER (ORDER BY created_at, id) AS running
FROM orders
WHERE status = 'paid'
ORDER BY created_at;      -- 100, 150, 350, 470, 770, 920

-- по клиентам: добавь PARTITION BY
SELECT customer_id, id, amount,
       sum(amount) OVER (PARTITION BY customer_id ORDER BY created_at, id) AS running
FROM orders WHERE status = 'paid';
```

Если в `OVER` есть `ORDER BY` и нет рамки, рамка по умолчанию — `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`: строки с одинаковым ключом сортировки считаются «одним моментом» и получают одинаковый итог. Поэтому `id` в `ORDER BY` — tie-breaker, без него заказы в одну секунду дадут одинаковый «нарастающий» итог.

### Скользящее среднее за 7 дней

Условие: средняя дневная выручка за текущий и 6 предыдущих дней.

```sql
WITH daily AS (
    SELECT created_at::date AS day, sum(amount) AS revenue
    FROM orders WHERE status = 'paid' GROUP BY 1
)
SELECT day, revenue,
       round(avg(revenue) OVER (ORDER BY day
             RANGE BETWEEN INTERVAL '6 days' PRECEDING AND CURRENT ROW), 2) AS avg_range,
       round(avg(revenue) OVER (ORDER BY day
             ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 2)                  AS avg_rows
FROM daily ORDER BY day;
```

На данных с пустыми днями: 20 января `avg_range = 50.00` (в окне только он сам), `avg_rows = 75.00` (захватил 5 января, это уже «предыдущая строка», а не «предыдущие 6 дней»). `ROWS` считает строки, `RANGE` — значения ключа. Для календарного окна при пропусках нужен `RANGE` с интервалом (PostgreSQL 11+, ключ — `date`/`timestamp`) либо добить дни через `generate_series` ([[#Выручка по месяцам с пустыми месяцами]]) и использовать `ROWS`.

### Ловушка LAST_VALUE

```sql
SELECT name, salary,
       last_value(name) OVER (ORDER BY salary) AS lv_default,
       last_value(name) OVER (ORDER BY salary
           ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS lv_full
FROM employees WHERE department_id = 2 ORDER BY salary;
-- lv_default: Zoya, Dina, Egor (всегда сама строка)   lv_full: Egor, Egor, Egor
```

Рамка по умолчанию заканчивается на текущей строке (с равными ей), поэтому «последнее» значение — это сама строка. Для `last_value` и `nth_value` всегда задавай рамку явно или переверни порядок и бери `first_value`.

### Рост выручки месяц к месяцу

```sql
WITH m AS (
    SELECT date_trunc('month', created_at)::date AS month, sum(amount) AS revenue
    FROM orders WHERE status = 'paid' GROUP BY 1
)
SELECT month, revenue,
       lag(revenue) OVER (ORDER BY month) AS prev,
       round(100.0 * (revenue - lag(revenue) OVER (ORDER BY month))
             / lag(revenue) OVER (ORDER BY month), 1) AS growth_pct
FROM m ORDER BY month;
-- 2025-01: 350, NULL   2025-02: 420, +20.0   2025-04: 150, -64.3
```

Первая строка получает `NULL`: `lag(x, 1, 0)` подставит дефолт, но деление на 0 упадёт, лучше оставить `NULL`. Ловушка: марта нет, и `lag` для апреля берёт февраль, а не пустой март. Если нужно сравнение с календарным предыдущим месяцем, сначала дополни месяцы ([[#Выручка по месяцам с пустыми месяцами]]). `LEAD` — то же в обратную сторону.

### Доля от итога

```sql
SELECT name, salary,
       round(100.0 * salary / sum(salary) OVER (), 1)                         AS pct_company,
       round(100.0 * salary / sum(salary) OVER (PARTITION BY department_id), 1) AS pct_dept
FROM employees ORDER BY id;
```

Пустое `OVER ()` — окно из всех строк. Это замена самосоединения с подзапросом `(SELECT sum(...))`; таблица читается один раз.

### Первый заказ каждого клиента

```sql
-- PostgreSQL-специфично: лаконично, и на индексе (customer_id, created_at) быстро
SELECT DISTINCT ON (customer_id) customer_id, id, created_at
FROM orders
ORDER BY customer_id, created_at, id;

-- переносимо
SELECT customer_id, id, created_at
FROM (SELECT *, row_number() OVER (PARTITION BY customer_id ORDER BY created_at, id) AS rn FROM orders) t
WHERE rn = 1;
```

`DISTINCT ON` оставляет первую строку каждой группы по заданному `ORDER BY`; `ORDER BY` обязан начинаться с тех же колонок, что и `DISTINCT ON`. Без `ORDER BY` выбор «первой» произволен. `ROW_NUMBER` нужен, когда нужны топ-N (не только 1) или код должен работать не только в PostgreSQL.

## Классические задачи

### Найти дубли и удалить

Условие: в `emails` повторяются адреса; найти повторы и оставить по одной строке (с наименьшим `id`).

```sql
SELECT email, count(*) FROM emails GROUP BY email HAVING count(*) > 1;   -- a@x.io 3, b@x.io 2

DELETE FROM emails
WHERE id IN (
    SELECT id FROM (
        SELECT id, row_number() OVER (PARTITION BY email ORDER BY id) AS rn
        FROM emails
    ) t
    WHERE rn > 1
);   -- DELETE 3, остаются id 1, 2, 4
```

Альтернатива — `DELETE ... USING`: `DELETE FROM emails a USING emails b WHERE a.email = b.email AND a.id > b.id;` — тот же результат, но самосоединение квадратично на больших группах дублей.

Если нет уникального ключа, идентифицируй строку по `ctid` (физический адрес строки):

```sql
DELETE FROM t
WHERE ctid IN (
    SELECT ctid FROM (
        SELECT ctid, row_number() OVER (PARTITION BY email) AS rn FROM t
    ) s WHERE rn > 1
);
```

> [!warning] ctid
> `ctid` меняется при `UPDATE` и `VACUUM FULL`, это не идентификатор, а адрес; используй его только внутри одного оператора. После очистки добавь `UNIQUE (email)`, иначе дубли вернутся. На больших таблицах удаляй пачками (`LIMIT` через `ctid` или диапазон `id`), иначе долгая транзакция раздует таблицу и блокирует vacuum.

### Дни подряд

Условие: пользователи, входившие 3 дня подряд и больше (gaps and islands).

Идея: внутри пользователя дни по возрастанию получают номера 1, 2, 3 ... У подряд идущих дней разница `день − номер` одинакова; пропуск сдвигает её. Разница — идентификатор серии.

```sql
SELECT user_id, min(day) AS from_day, max(day) AS to_day, count(*) AS days
FROM (
    SELECT user_id, day,
           day - (row_number() OVER (PARTITION BY user_id ORDER BY day))::int AS grp
    FROM logins
) t
GROUP BY user_id, grp
ORDER BY user_id, from_day;
-- 1: 03-01..03-03 (3)   1: 03-05..03-06 (2)   2: 03-01..03-02 (2)   4: 03-10..03-13 (4)
```

Для «не меньше 3 дней» замени финальное `SELECT` на `SELECT DISTINCT user_id ... GROUP BY user_id, grp HAVING count(*) >= 3` (пользователи 1 и 4). `date - int` даёт `date`; вариант `day - row_number() * interval '1 day'` тоже работает (получится `timestamp`), нужен для `timestamp`-ключей.

Если известна длина серии, короче через `LEAD`:

```sql
SELECT DISTINCT user_id
FROM (SELECT user_id, day, lead(day, 2) OVER (PARTITION BY user_id ORDER BY day) AS d2 FROM logins) t
WHERE d2 - day = 2;   -- 1 и 4
```

Работает, пока в `(user_id, day)` нет дублей: `lead` смотрит на строку, а не на значение. Для переменной длины серии или самой длинной серии — только `row_number`. Если дубли дней возможны, сначала `SELECT DISTINCT user_id, day`. Индекс: PK `(user_id, day)` отдаёт данные уже в нужном порядке.

### Пропуски в последовательности id

Условие: какие `id` пропущены в `tickets` (1, 2, 3, 6, 7, 10).

```sql
-- диапазоны пропусков
SELECT id + 1 AS gap_from, next_id - 1 AS gap_to
FROM (SELECT id, lead(id) OVER (ORDER BY id) AS next_id FROM tickets) t
WHERE next_id - id > 1;       -- 4..5, 8..9

-- каждое отсутствующее значение
SELECT g AS missing
FROM generate_series((SELECT min(id) FROM tickets), (SELECT max(id) FROM tickets)) g
LEFT JOIN tickets t ON t.id = g
WHERE t.id IS NULL;            -- 4, 5, 8, 9
```

Первый вариант быстрее на разреженных данных: `generate_series` создаёт строку на каждое возможное значение. Оба видят только пропуски внутри диапазона; пропуск в начале (нет `id = 1`) придётся добавить отдельно. Пропуски в `serial` — норма (откаты транзакций), это не баг данных.

### Выручка по месяцам с пустыми месяцами

Условие: оплаченная выручка по месяцам за январь–апрель, март должен быть с нулём.

```sql
SELECT m::date AS month,
       coalesce(sum(o.amount), 0) AS revenue,
       count(o.id) AS orders
FROM generate_series('2025-01-01'::date, '2025-04-01'::date, interval '1 month') AS m
LEFT JOIN orders o
       ON o.status = 'paid'
      AND o.created_at >= m
      AND o.created_at <  m + interval '1 month'
GROUP BY m
ORDER BY m;
-- 01: 350.00/3   02: 420.00/2   03: 0/0   04: 150.00/1
```

Без календаря обычный `GROUP BY date_trunc('month', created_at)` вернёт три строки: марта нет вовсе. Календарь слева, условия на `orders` в `ON` (иначе `LEFT JOIN` превратится в `INNER`, см. [[#Условие на правую таблицу в WHERE]]). Условие `>= m AND < m + interval` — диапазон по столбцу, использует индекс `orders (created_at)`; `date_trunc('month', created_at) = m` индекс не использует. `count(o.id)`, а не `count(*)`, иначе в пустом месяце будет 1.

### Медиана

```sql
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY salary) AS median,   -- 200
       percentile_disc(0.5) WITHIN GROUP (ORDER BY salary) AS disc,     -- 200
       avg(salary) AS avg                                                -- 209.44
FROM employees;

SELECT department_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY salary)
FROM employees GROUP BY department_id;   -- 1: 200, 2: 250, 3: 212.5
```

`percentile_cont` интерполирует между двумя средними значениями (в отделе 3 из 200 и 225 получилось 212.5), `percentile_disc` возвращает реально существующее значение. Агрегат сортирует группу целиком, на больших таблицах это дорого; приближённая медиана — отдельная история (`tdigest`, расширения). Вручную через `row_number` пишут только на диалектах без `percentile_cont`.

### Retention и когорты

Условие: доля клиентов, сделавших заказ в месяц, следующий за месяцем первого заказа.

```sql
WITH first_month AS (
    SELECT customer_id, min(date_trunc('month', created_at)) AS cohort
    FROM orders GROUP BY customer_id
), active AS (
    SELECT DISTINCT customer_id, date_trunc('month', created_at) AS month
    FROM orders
)
SELECT f.cohort::date,
       count(*) AS size,
       count(a.customer_id) AS returned,
       round(100.0 * count(a.customer_id) / count(*), 1) AS retention_pct
FROM first_month f
LEFT JOIN active a
       ON a.customer_id = f.customer_id
      AND a.month = f.cohort + interval '1 month'
GROUP BY f.cohort
ORDER BY f.cohort;
-- 2025-01: size 2, returned 2, 100.0   2025-02: size 1, returned 0, 0.0
```

Когорта — месяц первого заказа. `active` с `DISTINCT`, иначе два заказа в одном месяце посчитаются дважды. `LEFT JOIN` сохраняет когорты без вернувшихся (с нулём). `count(a.customer_id)`, а не `count(*)`, по той же причине, что выше. Для кривой по N месяцам добавь `month_offset` и `GROUP BY cohort, offset`.

### Отдел с максимальной средней зарплатой

Условие: отдел (или отделы) с максимальной средней зарплатой. Средние: Backend 212.5, Ops 212.5, Data 203.3.

```sql
SELECT name, avg_salary
FROM (
    SELECT d.name, avg(e.salary) AS avg_salary,
           rank() OVER (ORDER BY avg(e.salary) DESC) AS rnk
    FROM departments d
    JOIN employees e ON e.department_id = d.id
    GROUP BY d.id, d.name
) t
WHERE rnk = 1;   -- Backend и Ops
```

`ORDER BY avg DESC LIMIT 1` вернёт одного из двух (какого — зависит от плана) и тихо потеряет ничью. Окно над агрегатом легально: сначала `GROUP BY`, затем окно. Отдел без сотрудников (Design) выпал из-за `INNER JOIN`: если он нужен с нулём, `LEFT JOIN` и `coalesce(avg(...), 0)`.

## CTE и LATERAL

### CTE и MATERIALIZED

`WITH name AS (...)` — именованный подзапрос. С PostgreSQL 12 не рекурсивный CTE без побочных эффектов, на который ссылаются один раз, встраивается в запрос как обычный подзапрос, и фильтры внешнего запроса спускаются внутрь. Поведением управляют ключевые слова:

```sql
-- встроится: Seq Scan с обоими условиями (по умолчанию для одной ссылки)
WITH paid AS NOT MATERIALIZED (SELECT * FROM orders WHERE status = 'paid')
SELECT count(*) FROM paid WHERE customer_id = 1;

-- посчитается отдельно и сохранится: в плане CTE Scan
WITH paid AS MATERIALIZED (SELECT * FROM orders WHERE status = 'paid')
SELECT count(*) FROM paid WHERE customer_id = 1;
```

- `MATERIALIZED` — когда CTE дорогой, и на него несколько ссылок, или нужен барьер оптимизации (запретить спуск предиката, зафиксировать порядок).
- `NOT MATERIALIZED` — принудительно встроить даже при нескольких ссылках.
- До PostgreSQL 12 CTE всегда материализовался, и «CTE — барьер оптимизации» остался в головах. Сегодня это неверно по умолчанию.
- `WITH` с `INSERT` / `UPDATE` / `DELETE ... RETURNING` всегда выполняется один раз и не встраивается.

### Все подчинённые любой глубины

Условие: все прямые и непрямые подчинённые менеджера 1 с уровнем вложенности.

```sql
WITH RECURSIVE team AS (
    SELECT id, name, manager_id, 1 AS depth          -- якорь
    FROM employees WHERE id = 1
    UNION ALL
    SELECT e.id, e.name, e.manager_id, t.depth + 1   -- шаг: сотрудники, чей менеджер уже в team
    FROM employees e
    JOIN team t ON e.manager_id = t.id
)
SELECT id, name, depth FROM team WHERE id <> 1 ORDER BY depth, id;
-- 2, 3, 5 (depth 2); 4, 6, 7 (depth 3)
```

Работает по уровням: якорь, затем шаг применяется к результату предыдущего шага, пока не вернёт пусто. `UNION ALL` дешевле `UNION`, который дедуплицирует на каждом шаге. Обратная задача (цепочка начальников вверх) — поменяй условие соединения на `e.id = c.manager_id`; для Gleb получится Gleb, Boris, Anna. Индекс: `employees (manager_id)`, иначе каждый шаг сканирует таблицу целиком.

> [!warning] Циклы
> Если в данных есть цикл (Anna подчиняется Gleb, а Gleb — Anna через цепочку), рекурсия не остановится. Защита с PostgreSQL 14 — `CYCLE`:
> ```sql
> WITH RECURSIVE team AS (
>     SELECT id, name, manager_id FROM emp_loop WHERE id = 2
>     UNION ALL
>     SELECT e.id, e.name, e.manager_id
>     FROM emp_loop e JOIN team t ON e.manager_id = t.id
> ) CYCLE id SET is_cycle USING path
> SELECT id, name, is_cycle FROM team;
> -- строка Boris встречается второй раз с is_cycle = true, и обход дальше не идёт
> ```
> До 14 версии: копить путь массивом и отсекать посещённое.
> ```sql
> WITH RECURSIVE team AS (
>     SELECT id, name, manager_id, ARRAY[id] AS path FROM emp_loop WHERE id = 2
>     UNION ALL
>     SELECT e.id, e.name, e.manager_id, t.path || e.id
>     FROM emp_loop e JOIN team t ON e.manager_id = t.id
>     WHERE e.id <> ALL (t.path)
> )
> SELECT id, name, path FROM team;
> ```
> `emp_loop` — копия `employees`, где `manager_id` у Anna заменён на 4. `CYCLE` помечает повтор, массив просто не пускает его в результат.

### LATERAL как топ-N в группе

Условие: два последних заказа каждого клиента. `LATERAL` позволяет подзапросу в `FROM` ссылаться на столбцы предыдущей таблицы: подзапрос выполняется для каждого клиента.

```sql
SELECT c.name, o.id, o.amount, o.created_at::date
FROM customers c
CROSS JOIN LATERAL (
    SELECT id, amount, created_at
    FROM orders o
    WHERE o.customer_id = c.id
    ORDER BY created_at DESC, id DESC
    LIMIT 2
) o
ORDER BY c.id, o.created_at DESC;
-- Ann: 4, 2   Bob: 7, 5   Dan: 9, 8   (Cat пропала: CROSS JOIN)
```

Чтобы клиент без заказов остался, используй `LEFT JOIN LATERAL (...) o ON true`: Cat вернётся со `NULL`.

Индекс: `CREATE INDEX ON orders (customer_id, created_at DESC, id DESC)`. План для каждого клиента: `Index Only Scan ... Index Cond: (customer_id = c.id)` под `Limit`, то есть читаются только нужные две записи. Это выигрыш перед `ROW_NUMBER`, который пронумеровал бы все заказы. Но на таблице из нескольких крупных групп окно проще и не хуже; `LATERAL` выигрывает, когда групп мало (или внешний список мал), а строк в каждой много. Если групп много, а в каждой по паре строк, nested loop из тысяч проходов проиграет одному сканированию.

## Ловушки

### = NULL

`NULL` — «неизвестно»: любое сравнение с ним даёт `NULL`, а `WHERE` пропускает только истину.

```sql
SELECT count(*) FROM orders WHERE amount = NULL;                  -- 0, всегда
SELECT count(*) FROM orders WHERE amount IS NULL;                 -- 1
SELECT count(*) FROM orders WHERE amount <> 100;                  -- 7: заказ с NULL не вошёл
SELECT count(*) FROM orders WHERE amount IS DISTINCT FROM 100;    -- 8: NULL учтён
```

`IS DISTINCT FROM` / `IS NOT DISTINCT FROM` сравнивают с `NULL` как с обычным значением. Тот же корень у `NOT IN` ([[#Клиенты без заказов]]) и у `count(col)`.

### NOT IN с NULL

Разобрано выше: [[#Клиенты без заказов]]. Правило: `NOT EXISTS` всегда, `NOT IN` только с литеральным списком.

### BETWEEN и конец дня

`BETWEEN a AND b` включает обе границы. Для `timestamp` `'2025-01-31'` — это полночь, и заказ из 31 января 23:30 не попадёт.

```sql
SELECT count(*) FROM orders WHERE created_at BETWEEN '2025-01-01' AND '2025-01-31';          -- 2: потерян заказ 3
SELECT count(*) FROM orders WHERE created_at >= '2025-01-01' AND created_at < '2025-02-01';  -- 3
```

Правило: для времени всегда полуинтервал `[с, до)`. Он не требует угадывать `23:59:59.999999`, стыкуется без пересечений и пропусков при соседних периодах и работает с индексом по `created_at`.

### Пагинация: нестабильный ORDER BY

`ORDER BY salary DESC LIMIT 3 OFFSET 2` при ничьей по `salary` не гарантирует порядок равных строк. Между страницами один и тот же сотрудник может попасть на обе или ни на одну. Всегда добавляй уникальный ключ последним: `ORDER BY salary DESC, id`.

### OFFSET на больших смещениях

`OFFSET 100000 LIMIT 20` читает и отбрасывает 100 000 строк: время растёт линейно с номером страницы. Замена — keyset-пагинация: запоминай ключ последней строки и сравнивай кортежем.

```sql
SELECT id, created_at
FROM orders
WHERE (created_at, id) < ('2025-04-15 13:00+00', 8)   -- ключ последней строки прошлой страницы
ORDER BY created_at DESC, id DESC
LIMIT 3;                                               -- 7, 6, 5
```

Нужен индекс `(created_at DESC, id DESC)` и уникальный хвост ключа. Цена: нельзя перейти на страницу N, только «дальше / назад». Подробнее и про фильтры — [[PostgreSQL#Бизнес-кейс: фильтры в каталоге]].

### DISTINCT как лечение размножения

Если после `JOIN` строк больше, чем должно быть, и ты ставишь `DISTINCT`, значит, соединение построено неверно.

```sql
SELECT DISTINCT c.name FROM customers c JOIN logins l ON l.user_id = c.id;                    -- работает, но платит за все пары
SELECT c.name FROM customers c WHERE EXISTS (SELECT 1 FROM logins l WHERE l.user_id = c.id);  -- тот же результат
```

`DISTINCT` маскирует проблему: пары уже построены, дедупликация — лишняя сортировка или хеш, и он не спасает суммы (после размножения `sum` уже неверен, см. [[#Размножение строк при JOIN двух «один-ко-многим»]]). Нужны только значения левой таблицы — `EXISTS`; нужны агрегаты — агрегируй до `JOIN`.
