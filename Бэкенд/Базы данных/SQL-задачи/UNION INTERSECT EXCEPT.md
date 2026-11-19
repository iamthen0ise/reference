---
title: UNION INTERSECT EXCEPT
---

JOIN склеивает таблицы по горизонтали: к строке слева добавляются столбцы справа. Операции над множествами склеивают результаты двух запросов по вертикали: строки одного запроса дописываются к строкам другого, или из них выбираются общие, или одни вычитаются из других.

- `UNION` — строки из обоих запросов без повторов.
- `UNION ALL` — строки из обоих запросов как есть, с повторами.
- `INTERSECT` — строки, которые есть в обоих результатах.
- `EXCEPT` — строки первого результата, которых нет во втором.

Чтобы склейка была возможна, запросы должны возвращать одинаковое число столбцов совместимых типов. Имена столбцов берутся из первого запроса.

Самое частое применение — собрать одну ленту из разных источников. Например, история клиента Ann: её заказы и её входы одним списком по датам.

```sql
SELECT created_at::date AS day, 'order' AS kind FROM orders WHERE customer_id = 1
UNION ALL
SELECT day, 'login' FROM logins WHERE user_id = 1
ORDER BY day, kind;
```

| day | kind |
| --- | --- |
| 2025-01-05 | order |
| 2025-01-20 | order |
| 2025-02-03 | order |
| 2025-03-01 | login |
| 2025-03-02 | login |
| 2025-03-03 | login |
| 2025-03-05 | login |
| 2025-03-06 | login |

Здесь нужен именно `UNION ALL`. Простой `UNION` убирает повторы, а для этого ему приходится отсортировать или захешировать весь результат, то есть сделать лишнюю работу. К тому же он схлопнул бы два одинаковых события в одно. Поэтому по умолчанию пишут `UNION ALL`, а `UNION` — только когда повторы действительно нужно убрать. Разницу видно на списке клиентов, которые оплатили заказ или заходили после 10 марта:

```sql
SELECT customer_id FROM orders WHERE status = 'paid'
UNION ALL
SELECT user_id FROM logins WHERE day >= '2025-03-10'
ORDER BY 1;
```

| customer_id |
| --- |
| 1 |
| 1 |
| 2 |
| 2 |
| 4 |
| 4 |
| 4 |
| 4 |
| 4 |
| 4 |

```sql
SELECT customer_id FROM orders WHERE status = 'paid'
UNION
SELECT user_id FROM logins WHERE day >= '2025-03-10'
ORDER BY 1;
```

| customer_id |
| --- |
| 1 |
| 2 |
| 4 |

`UNION ALL` вернул 10 строк, по одной на каждый заказ и каждый вход. `UNION` свёл их к трём уникальным значениям. Если нужны «клиенты без повторов», `UNION` уместен, а если нужна лента событий или суммы по обеим частям, `UNION` потеряет строки, и итог окажется занижен. Отсутствие повторов `UNION` строит сортировкой или хешированием всего результата, и эту цену платят даже тогда, когда повторов в данных нет.

`ORDER BY` и `LIMIT` в конце относятся ко всему результату, а не к последнему запросу. Если нужно ограничить каждую часть отдельно, части берут в скобки:

```sql
(SELECT name FROM employees WHERE department_id = 1 ORDER BY salary DESC, id LIMIT 1)
UNION ALL
(SELECT name FROM employees WHERE department_id = 2 ORDER BY salary DESC, id LIMIT 1);
```

| name |
| --- |
| Anna |
| Egor |

`EXCEPT` и `INTERSECT` решают задачи «есть в A, нет в B» и «есть и там, и там». Например, отделы без сотрудников:

```sql
SELECT id FROM departments
EXCEPT
SELECT department_id FROM employees;
```

| id |
| --- |
| 4 |

У операций над множествами есть важное отличие от `NOT IN`: два `NULL` здесь считаются одинаковыми. Поэтому запрос «сотрудники, которые никому не менеджеры» через `EXCEPT` работает правильно, хотя в `manager_id` есть `NULL`. `NOT IN` на тех же данных возвращает пустоту ([[JOIN и подзапросы#Клиенты без заказов]]).

```sql
SELECT id FROM employees
EXCEPT
SELECT manager_id FROM employees
ORDER BY id;   -- Vera, Gleb, Egor, Zoya, Ivan
```

| id |
| --- |
| 3 |
| 4 |
| 6 |
| 7 |
| 9 |

Два `NULL` для этих операций равны, а совпадение столбцов проверяется по числу и типам:

```sql
SELECT NULL UNION SELECT NULL;       -- одна строка
SELECT NULL UNION ALL SELECT NULL;   -- две строки
```

```sql
SELECT id, name FROM departments UNION ALL SELECT id FROM employees;
```

```text title="Вывод"
ERROR:  each UNION query must have the same number of columns
LINE 1: SELECT id, name FROM departments UNION ALL SELECT id FROM em...
                                                          ^
```

```sql
SELECT id FROM departments UNION ALL SELECT name FROM employees;
```

```text title="Вывод"
ERROR:  UNION types integer and text cannot be matched
LINE 1: SELECT id FROM departments UNION ALL SELECT name FROM employees;
                                                    ^
```

Ограничение `EXCEPT` в том, что он сравнивает строки целиком: вернуть можно только те столбцы, по которым идёт сравнение. Если рядом нужны ещё и имена, удобнее `NOT EXISTS`.

`EXCEPT` и `INTERSECT` тоже убирают повторы. Варианты `EXCEPT ALL` и `INTERSECT ALL` учитывают количество: если в первом результате значение встречается трижды, а во втором один раз, `EXCEPT ALL` оставит его два раза. В `emails` адрес `a@x.io` встречается три раза, `b@x.io` — два, `c@x.io` — один:

```sql
SELECT email FROM emails EXCEPT SELECT 'a@x.io' ORDER BY 1;
```

| email |
| --- |
| b@x.io |
| c@x.io |

```sql
SELECT email FROM emails EXCEPT ALL SELECT 'a@x.io' ORDER BY 1;
```

| email |
| --- |
| a@x.io |
| a@x.io |
| b@x.io |
| b@x.io |
| c@x.io |

Обычный `EXCEPT` убрал адрес целиком, `EXCEPT ALL` вычел из трёх вхождений одно.

И последнее: `INTERSECT` связывает сильнее, чем `UNION` и `EXCEPT`, примерно как умножение сильнее сложения. `SELECT 1 UNION SELECT 2 INTERSECT SELECT 3` вернёт 1, а не пустоту, потому что сначала выполнится `2 INTERSECT 3`. Если важен другой порядок, ставь скобки.

```sql
SELECT 1 UNION SELECT 2 INTERSECT SELECT 3;
```

| ?column? |
| --- |
| 1 |

```sql
(SELECT 1 UNION SELECT 2) INTERSECT SELECT 3;
```

```text title="Вывод"
(0 rows)
```
