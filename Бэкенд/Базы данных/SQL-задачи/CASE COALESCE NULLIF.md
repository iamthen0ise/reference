---
title: CASE COALESCE NULLIF
---

Условные выражения нужны почти в каждом отчёте, и у каждого есть своя особенность при работе с `NULL`.

**CASE** выбирает значение по условиям. Условия проверяются сверху вниз, и срабатывает первое истинное, поэтому порядок веток важен: в примере ниже `salary >= 200` стоит после `salary >= 250`, иначе все senior попали бы в middle.

```sql
SELECT name, salary,
       CASE WHEN salary >= 250 THEN 'senior'
            WHEN salary >= 200 THEN 'middle'
            ELSE 'junior'
       END AS grade
FROM employees
ORDER BY salary DESC, id;
```

| name | salary | grade |
| --- | --- | --- |
| Anna | 300 | senior |
| Egor | 260 | senior |
| Dina | 250 | senior |
| Kira | 225 | middle |
| Boris | 200 | middle |
| Vera | 200 | middle |
| Ivan | 200 | middle |
| Gleb | 150 | junior |
| Zoya | 100 | junior |

Если ни одно условие не сработало, а `ELSE` нет, `CASE` возвращает `NULL`. Для сравнения одного выражения с константами есть короткая форма:

```sql
SELECT status,
       CASE status WHEN 'paid' THEN 'оплачен' WHEN 'new' THEN 'новый' END AS ru
FROM orders
GROUP BY status
ORDER BY status;
```

| status | ru |
| --- | --- |
| cancelled | NULL |
| new | новый |
| paid | оплачен |

Статус `cancelled` в `CASE` не описан, и без `ELSE` он дал `NULL`. Внутри агрегатов `CASE` даёт условную агрегацию ([[Агрегация#Условная агрегация и FILTER]]).

**COALESCE** возвращает первый аргумент, который не `NULL`. Чаще всего так подставляют значение по умолчанию: ноль вместо пустой суммы ([[Агрегация#NULL в агрегатах]]) или прочерк вместо отсутствующего менеджера.

```sql
SELECT e.name, coalesce(m.name, '—') AS manager
FROM employees e
LEFT JOIN employees m ON m.id = e.manager_id
WHERE e.department_id = 3
ORDER BY e.id;
```

| name | manager |
| --- | --- |
| Kira | — |
| Ivan | Kira |

**NULLIF(a, b)** делает обратное: возвращает `NULL`, если `a` равно `b`, а в остальных случаях возвращает `a`.

```sql
SELECT NULLIF(5, 5) AS equal, NULLIF(5, 6) AS different, NULLIF('', '') AS empty_str;
```

| equal | different | empty_str |
| --- | --- | --- |
| NULL | 5 | NULL |

Главное применение — защита от деления на ноль. `x / 0` падает с ошибкой, а `x / NULLIF(y, 0)` при нулевом `y` спокойно вернёт `NULL`. Условие: доля отменённых заказов относительно оплаченных по каждому клиенту. У Cat заказов нет, оплаченных ноль, и деление падает:

```sql
SELECT c.name,
       count(o.id) FILTER (WHERE o.status = 'cancelled') * 1.0
         / count(o.id) FILTER (WHERE o.status = 'paid') AS ratio
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id
GROUP BY c.id, c.name
ORDER BY c.id;
```

```text title="Вывод"
ERROR:  division by zero
```

Одна строка с нулевым знаменателем роняет весь запрос. С `NULLIF` она даёт `NULL`, а `coalesce` снаружи при необходимости подставит значение по смыслу (но 0 для «деления на ноль» годится не всегда: «доли нет» и «доля нулевая» — разные ответы):

```sql
SELECT c.name,
       round(count(o.id) FILTER (WHERE o.status = 'cancelled') * 1.0
             / NULLIF(count(o.id) FILTER (WHERE o.status = 'paid'), 0), 2) AS ratio
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id
GROUP BY c.id, c.name
ORDER BY c.id;
```

| name | ratio |
| --- | --- |
| Ann | 0.50 |
| Bob | 0.00 |
| Cat | NULL |
| Dan | 0.00 |

Второе применение `NULLIF` — превратить «пустое» значение в `NULL`, чтобы `coalesce` подставил значение по умолчанию и для пустых строк тоже:

```sql
SELECT v,
       coalesce(v, 'n/a')                AS only_coalesce,
       coalesce(NULLIF(trim(v), ''), 'n/a') AS with_nullif
FROM (VALUES ('x'), (''), ('  '), (NULL)) t(v);
```

| v | only_coalesce | with_nullif |
| --- | --- | --- |
| x | x | x |
|  |  | n/a |
|    |    | n/a |
| NULL | n/a | n/a |

Пустая строка и строка из пробелов в `coalesce` проходят как «значение», а с `NULLIF(trim(v), '')` превращаются в `n/a`. В задаче [[Оконные функции#Рост выручки месяц к месяцу]] так можно подстраховать деление на выручку прошлого месяца: `... / NULLIF(lag(revenue) OVER (ORDER BY month), 0)`.

**IS DISTINCT FROM** — сравнение, для которого `NULL` — обычное значение. `manager_id <> 1` для строк с `NULL` даёт «неизвестно», и `WHERE` их отбросит, а `manager_id IS DISTINCT FROM 1` для них истинно. В нашей таблице первое условие проходят 4 сотрудника, второе — 6: добавляются Anna и Kira, у которых менеджера нет.

```sql
SELECT name, manager_id,
       manager_id <> 1                 AS ne,
       manager_id IS DISTINCT FROM 1   AS is_distinct
FROM employees
WHERE id IN (1, 2, 8)
ORDER BY id;
```

| name | manager_id | ne | is_distinct |
| --- | --- | --- | --- |
| Anna | NULL | NULL | true |
| Boris | 1 | false | false |
| Kira | NULL | NULL | true |

В столбце `ne` у Anna и Kira стоит `NULL`, а не `true`, поэтому `WHERE manager_id <> 1` их отбрасывает. Обратная форма — `IS NOT DISTINCT FROM`: равенство, при котором `NULL` равен `NULL`.

**GREATEST и LEAST** выбирают наибольшее и наименьшее из нескольких значений одной строки, например `greatest(updated_at, created_at)`. В PostgreSQL они пропускают `NULL` (`greatest(1, NULL, 3)` даёт 3), а в MySQL любой `NULL` превращает результат в `NULL`. Это частый источник расхождений при переносе запросов.

```sql
SELECT greatest(1, NULL, 3) AS gr, least(1, NULL, 3) AS le, greatest(NULL::int, NULL) AS all_null;
```

| gr | le | all_null |
| --- | --- | --- |
| 3 | 1 | NULL |

`NULL` получится, только если все аргументы `NULL`.

Ещё одна особенность `CASE`: ветки вычисляются лениво, но константные выражения PostgreSQL упрощает на этапе планирования, до чтения данных. Поэтому `CASE` не всегда защищает от ошибки в «невыбранной» ветке:

```sql
SELECT CASE WHEN id > 0 THEN 1 ELSE 1 / 0 END FROM employees;
```

```text title="Вывод"
ERROR:  division by zero
```

Выражение `1 / 0` константное, и база свернула его при планировании, хотя ветка `ELSE` ни для одной строки не нужна. Если знаменатель берётся из столбца, ветку никто не вычислит, пока она не понадобится: `CASE WHEN id > 0 THEN 1 ELSE 1 / (id - id) END` вернёт 1 без ошибки. Для делителя-столбца надёжнее всё равно `NULLIF`.
