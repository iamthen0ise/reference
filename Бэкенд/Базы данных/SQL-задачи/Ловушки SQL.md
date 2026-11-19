---
title: Ловушки SQL
---

## = NULL

`NULL` означает «неизвестно». Любое сравнение с ним даёт не «истина» и не «ложь», а `NULL`, а `WHERE` пропускает только истину.

```sql
SELECT count(*) FILTER (WHERE amount = NULL)                AS eq_null,       -- 0, всегда
       count(*) FILTER (WHERE amount IS NULL)               AS is_null,       -- заказ 9
       count(*) FILTER (WHERE amount <> 100)                AS ne_100,        -- заказ с NULL не вошёл
       count(*) FILTER (WHERE amount IS DISTINCT FROM 100)  AS distinct_100   -- NULL учтён
FROM orders;
```

| eq_null | is_null | ne_100 | distinct_100 |
| --- | --- | --- | --- |
| 0 | 1 | 7 | 8 |

Логика здесь трёхзначная: у результата сравнения три значения, «истина», «ложь» и «неизвестно» (`NULL`). `AND` и `OR` сводят неизвестное к определённому ответу, только когда второй операнд его решает: `NULL AND false` — ложь, `NULL OR true` — истина. Остальные комбинации остаются неизвестными, и `NOT` от неизвестного тоже неизвестно.

```sql
SELECT NULL = NULL                  AS eq,
       NULL IS NOT DISTINCT FROM NULL AS not_distinct,
       (NULL AND false)             AS and_false,
       (NULL OR true)               AS or_true,
       NOT NULL::boolean            AS not_null;
```

| eq | not_distinct | and_false | or_true | not_null |
| --- | --- | --- | --- | --- |
| NULL | true | false | true | NULL |

Для проверки на `NULL` существует `IS NULL`. Операторы `IS DISTINCT FROM` и `IS NOT DISTINCT FROM` сравнивают значения, считая `NULL` обычным значением. Тот же корень у ловушки с `NOT IN` ([[JOIN и подзапросы#Клиенты без заказов]]) и у того, что `count(col)` пропускает `NULL`.

## NOT IN с NULL

Подробно разобрано выше: [[JOIN и подзапросы#Клиенты без заказов]]. Правило: всегда `NOT EXISTS`, а `NOT IN` только с литеральным списком значений.

`x NOT IN (a, b)` раскрывается в `x <> a AND x <> b`. Если один из элементов `NULL`, то `x <> NULL` неизвестно, и всё условие не может стать истинным:

```sql
SELECT 1 NOT IN (2, 3)    AS no_null,     -- нет совпадений, NULL-ов нет
       1 NOT IN (2, NULL) AS with_null,   -- совпадения нет, но и уверенности нет
       1 NOT IN (1, NULL) AS has_match;   -- совпадение есть: ложь без вариантов
```

| no_null | with_null | has_match |
| --- | --- | --- |
| true | NULL | false |

На таблице: сотрудники, которые никому не менеджеры. В `employees.manager_id` есть `NULL` (у Anna и Kira), поэтому `NOT IN` молча возвращает пустоту, а `NOT EXISTS` отвечает верно.

```sql
SELECT name FROM employees
WHERE id NOT IN (SELECT manager_id FROM employees);
```

```text title="Вывод"
(0 rows)
```

```sql
SELECT name FROM employees e
WHERE NOT EXISTS (SELECT 1 FROM employees s WHERE s.manager_id = e.id)
ORDER BY id;
```

| name |
| --- |
| Vera |
| Gleb |
| Egor |
| Zoya |
| Ivan |

Если очень хочется `NOT IN`, исключи `NULL` в подзапросе: `WHERE manager_id IS NOT NULL` даёт тот же ответ. Но ограничение держится только на дисциплине, а `NOT EXISTS` от него не зависит.

## COUNT(*) и COUNT(col) после LEFT JOIN

`count(*)` считает строки, `count(col)` — строки, где `col` не `NULL`. После `LEFT JOIN` у строки без пары вся правая часть `NULL`, но сама строка есть, поэтому `count(*)` насчитает единицу там, где должен быть ноль.

```sql
SELECT d.name, count(*) AS count_star, count(e.id) AS count_id
FROM departments d
LEFT JOIN employees e ON e.department_id = d.id
GROUP BY d.id, d.name
ORDER BY d.id;
```

| name | count_star | count_id |
| --- | --- | --- |
| Backend | 4 | 4 |
| Data | 3 | 3 |
| Ops | 2 | 2 |
| Design | 1 | 0 |

Отдел Design пуст, но `count(*)` показывает в нём одного сотрудника. Считай по столбцу правой таблицы, который не бывает `NULL` в настоящих строках (обычно первичный ключ). Тот же корень у среднего: `avg(col)` делит на число ненулевых значений, и `sum(col) / count(*)` с ним не совпадает ([[Агрегация#NULL в агрегатах]]).

```sql
SELECT count(*)                  AS count_star,
       count(amount)             AS count_amount,
       avg(amount)::numeric(8,2) AS avg_amount,
       (sum(amount) / count(*))::numeric(8,2) AS sum_over_all
FROM orders;
```

| count_star | count_amount | avg_amount | sum_over_all |
| --- | --- | --- | --- |
| 9 | 8 | 133.75 | 118.89 |

## Целочисленное деление

Если оба операнда целые, `/` отбрасывает дробную часть (к нулю), и ошибки не будет. `count`, `sum` по `int` и литералы без точки целые, поэтому доли и проценты на них тихо превращаются в 0.

```sql
SELECT 7 / 2            AS int_div,
       -7 / 2           AS neg_div,
       7 % 2            AS remainder,
       7 / 2.0          AS numeric_div,
       7::numeric / 2   AS cast_div,
       (7 / 2) * 2.0    AS divide_first,
       7 * 2.0 / 2      AS multiply_first;
```

| int_div | neg_div | remainder | numeric_div | cast_div | divide_first | multiply_first |
| --- | --- | --- | --- | --- | --- | --- |
| 3 | -3 | 1 | 3.5000000000000000 | 3.5000000000000000 | 6.0 | 7.0000000000000000 |

Хватает одного нецелого операнда, но он должен оказаться в выражении до деления: `(7 / 2) * 2.0` уже поделил целые и получил 6.0, а не 7. Про долю оплаченных заказов — [[Агрегация#Доля и целочисленное деление]].

## LEFT JOIN: условие в ON и в WHERE

Условие на правую таблицу в `WHERE` выбрасывает строки, у которых справа `NULL`, то есть именно те, ради которых нужен `LEFT JOIN`. В `ON` оно только решает, какие пары считать подходящими. Клиенты 3 (Cat, без заказов) и 4 (Dan, у него два оплаченных заказа и один новый):

```sql
-- условие в ON: Cat осталась, подходящие заказы Dan присоединились
SELECT c.name, o.id, o.status
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id AND o.status = 'paid'
WHERE c.id IN (3, 4)
ORDER BY c.id, o.id;
```

| name | id | status |
| --- | --- | --- |
| Cat | NULL | NULL |
| Dan | 6 | paid |
| Dan | 8 | paid |

```sql
-- то же условие в WHERE: Cat пропала, LEFT JOIN превратился в INNER
SELECT c.name, o.id, o.status
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id
WHERE o.status = 'paid' AND c.id IN (3, 4)
ORDER BY c.id, o.id;
```

| name | id | status |
| --- | --- | --- |
| Dan | 6 | paid |
| Dan | 8 | paid |

Условия на левую таблицу (`c.id IN (3, 4)`) безопасно писать в `WHERE`. Исключение: если нужно оставить строки, которые пары не нашли, пишут `WHERE o.id IS NULL` — это anti-join ([[JOIN и подзапросы#Клиенты без заказов]]). Разбор на задаче — [[JOIN и подзапросы#Условие на правую таблицу в WHERE]].

## BETWEEN и конец дня

`BETWEEN a AND b` включает обе границы. Для `timestamp` запись `'2025-01-31'` означает полночь начала 31 января, поэтому заказ из 31 января 23:30 в диапазон не попадёт.

```sql
SELECT count(*) FROM orders WHERE created_at BETWEEN '2025-01-01' AND '2025-01-31';          -- 2: потерян заказ 3
SELECT count(*) FROM orders WHERE created_at >= '2025-01-01' AND created_at < '2025-02-01';  -- 3
```

Правило: для времени всегда используй полуинтервал «с границы включительно, до границы не включая». Он не требует угадывать `23:59:59.999999`, стыкуется с соседними периодами без пересечений и пропусков и работает с индексом по `created_at`.

## Пагинация: нестабильный ORDER BY

Запрос `ORDER BY salary DESC LIMIT 3 OFFSET 2` при ничьей по `salary` не гарантирует порядок равных строк. Между запросами порядок может измениться, и один и тот же сотрудник попадёт на две страницы, а другой не попадёт ни на одну. Всегда добавляй уникальный ключ последним в сортировку: `ORDER BY salary DESC, id`.

## OFFSET на больших смещениях

`OFFSET 100000 LIMIT 20` читает и выбрасывает 100 000 строк, поэтому время растёт линейно вместе с номером страницы. Замена — keyset-пагинация: запомни ключ последней строки текущей страницы и в следующем запросе потребуй строки строго «после» этого ключа. Ключ сравнивается кортежем из двух столбцов: `(created_at, id)`.

```sql
SELECT id, created_at
FROM orders
WHERE (created_at, id) < ('2025-04-15 13:00+00', 8)   -- ключ последней строки прошлой страницы
ORDER BY created_at DESC, id DESC
LIMIT 3;                                               -- 7, 6, 5
```

Нужен индекс `(created_at DESC, id DESC)` и уникальный хвост ключа (`id`), иначе при равных `created_at` строки потеряются или повторятся. Цена такого подхода: нельзя перейти сразу на страницу N, можно только «дальше» и «назад». Подробнее и про фильтры — [[Индексы#Бизнес-кейс: фильтры в каталоге]].

## DISTINCT как лечение размножения

Если после `JOIN` строк оказалось больше, чем должно быть, и ты ставишь `DISTINCT`, значит, соединение построено неверно.

```sql
SELECT DISTINCT c.name FROM customers c JOIN logins l ON l.user_id = c.id;                    -- работает, но платит за все пары
SELECT c.name FROM customers c WHERE EXISTS (SELECT 1 FROM logins l WHERE l.user_id = c.id);  -- тот же результат
```

`DISTINCT` маскирует проблему: пары уже построены, а дедупликация — это дополнительная сортировка или хеширование. И он не спасает суммы: после размножения строк `sum` уже посчитан неверно ([[JOIN и подзапросы#Размножение строк при JOIN двух «один-ко-многим»]]). Если нужны только значения левой таблицы, используй `EXISTS`. Если нужны агрегаты, агрегируй каждую сторону до `JOIN`.
