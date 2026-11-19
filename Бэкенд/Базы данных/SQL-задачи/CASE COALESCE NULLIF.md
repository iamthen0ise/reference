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
-- senior: Anna, Egor, Dina;  middle: Kira, Boris, Vera, Ivan;  junior: Gleb, Zoya
```

Если ни одно условие не сработало, а `ELSE` нет, `CASE` возвращает `NULL`. Для сравнения одного выражения с константами есть короткая форма: `CASE status WHEN 'paid' THEN 'оплачен' WHEN 'new' THEN 'новый' END`. Внутри агрегатов `CASE` даёт условную агрегацию ([[Агрегация#Условная агрегация и FILTER]]).

**COALESCE** возвращает первый аргумент, который не `NULL`. Чаще всего так подставляют значение по умолчанию: ноль вместо пустой суммы ([[Агрегация#NULL в агрегатах]]) или прочерк вместо отсутствующего менеджера.

```sql
SELECT e.name, coalesce(m.name, '—') AS manager
FROM employees e
LEFT JOIN employees m ON m.id = e.manager_id
WHERE e.department_id = 3
ORDER BY e.id;   -- Kira —, Ivan Kira
```

**NULLIF(a, b)** делает обратное: возвращает `NULL`, если `a` равно `b`, а в остальных случаях возвращает `a`. Главное применение — защита от деления на ноль. `x / 0` падает с ошибкой, а `x / NULLIF(y, 0)` при нулевом `y` спокойно вернёт `NULL`. В задаче [[Оконные функции#Рост выручки месяц к месяцу]] так можно подстраховать деление на выручку прошлого месяца: `... / NULLIF(lag(revenue) OVER (ORDER BY month), 0)`.

**IS DISTINCT FROM** — сравнение, для которого `NULL` — обычное значение. `manager_id <> 1` для строк с `NULL` даёт «неизвестно», и `WHERE` их отбросит, а `manager_id IS DISTINCT FROM 1` для них истинно. В нашей таблице первое условие проходят 4 сотрудника, второе — 6: добавляются Anna и Kira, у которых менеджера нет. Обратная форма — `IS NOT DISTINCT FROM`: равенство, при котором `NULL` равен `NULL`.

**GREATEST и LEAST** выбирают наибольшее и наименьшее из нескольких значений одной строки, например `greatest(updated_at, created_at)`. В PostgreSQL они пропускают `NULL` (`greatest(1, NULL, 3)` даёт 3), а в MySQL любой `NULL` превращает результат в `NULL`. Это частый источник расхождений при переносе запросов.
