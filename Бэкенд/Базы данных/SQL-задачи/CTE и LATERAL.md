---
title: CTE и LATERAL
---

## CTE и ключевое слово MATERIALIZED

`WITH имя AS (...)` создаёт CTE — именованный подзапрос, на который можно ссылаться в основном запросе. С PostgreSQL 12 нерекурсивный CTE без побочных эффектов, на который ссылаются один раз, встраивается в основной запрос как обычный подзапрос, и фильтры внешнего запроса спускаются внутрь. Этим поведением управляют ключевые слова: `MATERIALIZED` («вычисли CTE отдельно и сохрани результат») и `NOT MATERIALIZED` («встрой в запрос»).

```sql
-- встроится: Seq Scan с обоими условиями (по умолчанию для одной ссылки)
WITH paid AS NOT MATERIALIZED (SELECT * FROM orders WHERE status = 'paid')
SELECT count(*) FROM paid WHERE customer_id = 1;

-- посчитается отдельно и сохранится: в плане CTE Scan
WITH paid AS MATERIALIZED (SELECT * FROM orders WHERE status = 'paid')
SELECT count(*) FROM paid WHERE customer_id = 1;
```

Планы получены через `EXPLAIN (COSTS OFF)` с теми же запросами. Первый, `NOT MATERIALIZED`:

```text title="Вывод"
 Aggregate
   ->  Seq Scan on orders
         Filter: ((status = 'paid'::text) AND (customer_id = 1))
```

Второй, `MATERIALIZED`:

```text title="Вывод"
 Aggregate
   CTE paid
     ->  Seq Scan on orders
           Filter: (status = 'paid'::text)
   ->  CTE Scan on paid
         Filter: (customer_id = 1)
```

Seq Scan — последовательное чтение всей таблицы, CTE Scan — чтение сохранённого результата CTE. В первом случае фильтр `customer_id = 1` объединяется с условием внутри CTE, во втором CTE сначала целиком выбирает оплаченные заказы, и только потом фильтруется.

- `MATERIALIZED` нужен, когда CTE дорогой и на него несколько ссылок (чтобы не считать дважды), либо когда нужен барьер оптимизации: запретить спуск условий внутрь или зафиксировать порядок вычисления.
- `NOT MATERIALIZED` принудительно встраивает CTE, даже если на него несколько ссылок.
- До PostgreSQL 12 CTE всегда материализовался, и утверждение «CTE — барьер оптимизации» осталось в головах. Сегодня по умолчанию это неверно.
- `WITH` с `INSERT`, `UPDATE` или `DELETE ... RETURNING` всегда выполняется ровно один раз и не встраивается.

## Все подчинённые любой глубины

Условие: найти всех прямых и непрямых подчинённых менеджера 1 с уровнем вложенности.

Идея: рекурсивный CTE. Он состоит из двух частей, склеенных `UNION ALL`: якорь (стартовая строка: сам менеджер 1) и шаг (найти сотрудников, чей менеджер уже попал в результат). База повторяет шаг по уровням: каждый раз применяет его к строкам, найденным на предыдущем шаге, и останавливается, когда шаг вернул пусто.

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
```

| id | name | depth |
| --- | --- | --- |
| 2 | Boris | 2 |
| 3 | Vera | 2 |
| 5 | Dina | 2 |
| 4 | Gleb | 3 |
| 6 | Egor | 3 |
| 7 | Zoya | 3 |

`UNION ALL` дешевле `UNION`: второй дедуплицирует результат на каждом шаге. Сам менеджер (id 1) исключён из результата условием `WHERE id <> 1`.

Обратная задача — цепочка начальников вверх. Поменяй условие соединения в шаге на `e.id = c.manager_id`, где `c` — имя рекурсивного CTE (в примере выше он назван `team`): для Gleb получится Gleb, Boris, Anna.

```sql
WITH RECURSIVE chain AS (
    SELECT id, name, manager_id, 1 AS lvl
    FROM employees WHERE id = 4
    UNION ALL
    SELECT e.id, e.name, e.manager_id, c.lvl + 1
    FROM employees e
    JOIN chain c ON e.id = c.manager_id          -- идём вверх: берём менеджера текущей строки
)
SELECT id, name, lvl FROM chain ORDER BY lvl;
```

| id | name | lvl |
| --- | --- | --- |
| 4 | Gleb | 1 |
| 2 | Boris | 2 |
| 1 | Anna | 3 |

Чтобы вывести всю иерархию деревом, в рекурсии копят путь из `id` и сортируют по нему: потомки встают сразу под родителем. Якорь здесь — все топ-менеджеры (`manager_id IS NULL`).

```sql
WITH RECURSIVE team AS (
    SELECT id, name, 1 AS depth, ARRAY[id] AS path
    FROM employees WHERE manager_id IS NULL
    UNION ALL
    SELECT e.id, e.name, t.depth + 1, t.path || e.id
    FROM employees e
    JOIN team t ON e.manager_id = t.id
)
SELECT repeat('- ', depth - 1) || name AS tree, depth, path
FROM team
ORDER BY path;
```

| tree | depth | path |
| --- | --- | --- |
| Anna | 1 | {1} |
| - Boris | 2 | {1,2} |
| - - Gleb | 3 | {1,2,4} |
| - Vera | 2 | {1,3} |
| - Dina | 2 | {1,5} |
| - - Egor | 3 | {1,5,6} |
| - - Zoya | 3 | {1,5,7} |
| Kira | 1 | {8} |
| - Ivan | 2 | {8,9} |

Дефисы в начале ячейки показывают вложенность. Тот же путь служит и защитой от циклов (см. ниже), и сортировкой «в глубину».

Рекурсивный CTE годится и как генератор рядов. Условие остановки в шаге ограничивает рекурсию:

```sql
WITH RECURSIVE n(i) AS (
    SELECT 1
    UNION ALL
    SELECT i + 1 FROM n WHERE i < 5
)
SELECT i FROM n;
```

| i |
| --- |
| 1 |
| 2 |
| 3 |
| 4 |
| 5 |

Без `WHERE i < 5` рекурсия бесконечна, но запрос с `LIMIT` в основном запросе всё равно завершится: PostgreSQL вычисляет CTE лениво и останавливается, когда набрано нужное число строк.

```sql
WITH RECURSIVE n(i) AS (
    SELECT 1 UNION ALL SELECT i + 1 FROM n
)
SELECT i FROM n LIMIT 3;
```

| i |
| --- |
| 1 |
| 2 |
| 3 |

На это не стоит полагаться в боевом коде: достаточно добавить `ORDER BY` или агрегат, и запрос зациклится, потому что ему нужны все строки.

Индекс: `employees (manager_id)`. Без него на каждом шаге таблица сканируется целиком.

> [!warning] Циклы
> Если в данных есть цикл (Anna подчиняется Gleb, а Gleb через цепочку подчиняется Anna), рекурсия не остановится. Защита с PostgreSQL 14 — предложение `CYCLE`: оно запоминает пройденные значения и помечает строку, которая встретилась второй раз.
> ```sql
> WITH RECURSIVE team AS (
>     SELECT id, name, manager_id FROM emp_loop WHERE id = 2
>     UNION ALL
>     SELECT e.id, e.name, e.manager_id
>     FROM emp_loop e JOIN team t ON e.manager_id = t.id
> ) CYCLE id SET is_cycle USING path
> SELECT id, name, is_cycle FROM team;
> ```
> | id | name | is_cycle |
> | --- | --- | --- |
> | 2 | Boris | false |
> | 4 | Gleb | false |
> | 1 | Anna | false |
> | 2 | Boris | true |
> | 3 | Vera | false |
> | 5 | Dina | false |
> | 6 | Egor | false |
> | 7 | Zoya | false |
>
> Строка Boris встречается второй раз с `is_cycle = true`, и обход дальше не идёт (Vera, Dina и остальные — потомки Anna, они пришли по другой ветке).
> До версии 14 путь копили массивом и отсекали уже посещённые узлы вручную.
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
> | id | name | path |
> | --- | --- | --- |
> | 2 | Boris | {2} |
> | 4 | Gleb | {2,4} |
> | 1 | Anna | {2,4,1} |
> | 3 | Vera | {2,4,1,3} |
> | 5 | Dina | {2,4,1,5} |
> | 6 | Egor | {2,4,1,5,6} |
> | 7 | Zoya | {2,4,1,5,7} |
>
> `emp_loop` — копия `employees`, где `manager_id` у Anna заменён на 4. `CYCLE` помечает повторный заход в узел и не идёт дальше, массив просто не пускает повторный узел в результат.

## LATERAL как топ-N в группе

Условие: два последних заказа каждого клиента.

Идея: `LATERAL` разрешает подзапросу в `FROM` ссылаться на столбцы таблицы, стоящей левее. Поэтому подзапрос выполняется отдельно для каждого клиента: берёт заказы именно этого клиента, сортирует от новых к старым и отрезает два через `LIMIT 2`.

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
```

| name | id | amount | created_at |
| --- | --- | --- | --- |
| Ann | 4 | 70.00 | 2025-02-03 |
| Ann | 2 | 50.00 | 2025-01-20 |
| Bob | 7 | 80.00 | 2025-04-02 |
| Bob | 5 | 120.00 | 2025-02-14 |
| Dan | 9 | NULL | 2025-04-20 |
| Dan | 8 | 150.00 | 2025-04-15 |

Cat пропала: `CROSS JOIN` отбрасывает клиента, для которого подзапрос вернул пусто. Чтобы клиент без заказов остался в результате, используй `LEFT JOIN LATERAL (...) o ON true`: Cat вернётся со `NULL` в столбцах заказа. (`ON true` нужен потому, что синтаксис `LEFT JOIN` требует условие, а вся фильтрация уже внутри подзапроса.)

```sql
SELECT c.name, o.id, o.amount
FROM customers c
LEFT JOIN LATERAL (
    SELECT id, amount, created_at
    FROM orders o
    WHERE o.customer_id = c.id
    ORDER BY created_at DESC, id DESC
    LIMIT 2
) o ON true
ORDER BY c.id, o.created_at DESC;
```

| name | id | amount |
| --- | --- | --- |
| Ann | 4 | 70.00 |
| Ann | 2 | 50.00 |
| Bob | 7 | 80.00 |
| Bob | 5 | 120.00 |
| Cat | NULL | NULL |
| Dan | 9 | NULL |
| Dan | 8 | 150.00 |

`amount` у заказа 9 равен `NULL` в самих данных, а у Cat `NULL` пришёл из-за отсутствия строки. Отличить их можно по `o.id`.

Индекс: `CREATE INDEX ON orders (customer_id, created_at DESC, id DESC)`. План для каждого клиента: `Index Only Scan ... Index Cond: (customer_id = c.id)` под `Limit`, то есть читаются только нужные две записи индекса. Это выигрыш перед `ROW_NUMBER`, который пронумеровал бы все заказы.

Когда `LATERAL` не нужен: на небольшой таблице окно проще и не хуже. `LATERAL` выигрывает, когда групп мало (или внешний список мал), а строк в каждой много. Если групп много, а в каждой по паре строк, цикл из тысяч отдельных проходов (nested loop, вложенный цикл) проиграет одному сканированию таблицы.
