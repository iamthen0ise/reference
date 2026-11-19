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
-- 2, 3, 5 (depth 2); 4, 6, 7 (depth 3)
```

`UNION ALL` дешевле `UNION`: второй дедуплицирует результат на каждом шаге. Сам менеджер (id 1) исключён из результата условием `WHERE id <> 1`.

Обратная задача — цепочка начальников вверх. Поменяй условие соединения в шаге на `e.id = c.manager_id`, где `c` — имя рекурсивного CTE (в примере выше он назван `team`): для Gleb получится Gleb, Boris, Anna.

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
> -- строка Boris встречается второй раз с is_cycle = true, и обход дальше не идёт
> ```
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
-- Ann: 4, 2   Bob: 7, 5   Dan: 9, 8   (Cat пропала: CROSS JOIN)
```

`CROSS JOIN` отбрасывает клиента, для которого подзапрос вернул пусто. Чтобы клиент без заказов остался в результате, используй `LEFT JOIN LATERAL (...) o ON true`: Cat вернётся со `NULL` в столбцах заказа. (`ON true` нужен потому, что синтаксис `LEFT JOIN` требует условие, а вся фильтрация уже внутри подзапроса.)

Индекс: `CREATE INDEX ON orders (customer_id, created_at DESC, id DESC)`. План для каждого клиента: `Index Only Scan ... Index Cond: (customer_id = c.id)` под `Limit`, то есть читаются только нужные две записи индекса. Это выигрыш перед `ROW_NUMBER`, который пронумеровал бы все заказы.

Когда `LATERAL` не нужен: на небольшой таблице окно проще и не хуже. `LATERAL` выигрывает, когда групп мало (или внешний список мал), а строк в каждой много. Если групп много, а в каждой по паре строк, цикл из тысяч отдельных проходов (nested loop, вложенный цикл) проиграет одному сканированию таблицы.
