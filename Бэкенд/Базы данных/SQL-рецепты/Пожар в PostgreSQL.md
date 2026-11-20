---
title: Пожар в PostgreSQL
---

База тормозит или зависла, приложение отдаёт таймауты. Что смотреть и в каком порядке: кто выполняется, кто кого держит, как снять блокировку и не сделать хуже, не кончились ли соединения. Пожар воспроизводится в Docker несколькими сессиями на данных «Библиотеки»: одна держит строку, другая ждёт её, третья пытается сменить схему, четвёртая просто читает.

Рядом: [[Схема библиотеки]], [[Эффективность индексов]], [[Блокировки]], [[Транзакции и уровни изоляции]], [[Миграции без простоя]], [[EXPLAIN]].

## Чек-лист пожара

1. Посмотреть, что выполняется: `pg_stat_activity`, колонки `state`, `wait_event_type`, `wait_event`, возраст запроса и транзакции ([раздел](#что-выполняется-сейчас)).
2. Найти, кто кого блокирует: `pg_blocking_pids` и цепочка до корня ([раздел](#кто-кого-блокирует)). Чинить надо корень, а не очередь за ним.
3. Понять, что делает корень: активный запрос или `idle in transaction`. Во втором случае запрос отменять нечего.
4. Снять мягко: `pg_cancel_backend` отменяет текущий запрос, сессия остаётся. Отменять нужно запрос корня, если он у него есть; ожидающих отменять смысла нет, пожар они не тушат ([раздел](#как-снять)).
5. Корень простаивает в транзакции или отмена не помогла: `pg_terminate_backend` корня. Транзакция откатится, изменения пропадут.
6. Проверить соединения: сколько занято и в каких состояниях, против `max_connections` ([раздел](#соединения-и-лимит)).
7. После пожара: `lock_timeout` для миграций, `statement_timeout` и `idle_in_transaction_session_timeout` для приложения ([раздел](#таймауты-против-пожара)).

## Как читать страницу

Порядок запуска на `small` (блокировки не зависят от размера таблицы): `make sql-up SEED=small`; блок `row-before.sql`; в фоне по очереди `fire-holder.sql`, `fire-waiter.sql`, `fire-alter.sql`, `fire-reader.sql` (между запусками пауза в секунду); дальше по порядку страницы `who-is-running.sql`, `who-blocks-whom.sql`, `blocking-chain.sql`, `connections-by-state.sql`, `connections-limit.sql`, `cancel-waiter.sql`, `cancel-holder.sql`, `terminate-holder.sql`, `row-after.sql`, `timeout-settings.sql`; снова в фоне `fire-holder-2.sql` (тот же держатель под другим именем), затем `statement-timeout.sql`, `lock-timeout.sql`, `terminate-holder-2.sql`; в фоне `idle-holder.sql`, сразу `idle-seen.sql`, через 5 секунд `idle-gone.sql`.

- Фоновый блок запускается `make sql-bg PAGE="$P" TITLE="<имя>.sql"`, обычный — `make sql-block PAGE="$P" TITLE="<имя>.sql"`, где `P="Бэкенд/Базы данных/SQL-рецепты/Пожар в PostgreSQL.md"`. `sql-bg` копирует блок в контейнер, запускает `psql -f` в фоне и возвращается, когда блок начал работу: сессия в `pg_stat_activity` держит открытую транзакцию, выполняет запрос или ждёт блокировку (одних `SET` и `BEGIN` недостаточно). Её `application_name` равно имени блока без `.sql`: по нему сессии и различаются в выводе.
- Сессии не остаются висеть. У каждой фоновой сессии срок жизни ограничивает сервер, а не терпение: `idle_in_transaction_session_timeout` у держателя строки, `lock_timeout` и `statement_timeout` у остальных, потолок 50 секунд. Это следует из таймаутов: самая долгая из четырёх сессий живёт 55 секунд (`\! sleep 55`), так что за минуту после запуска не остаётся ни сессий, ни фоновых `psql`.
- Из-за этого потолка на сценарий пожара есть около 50 секунд от запуска `fire-holder.sql`. Не успели, и сервер сам закрыл держателя, тогда `make sql-up SEED=small` и всё сначала.
- Держатель удерживает транзакцию так: после `BEGIN` и `UPDATE` клиент не завершает работу, а ждёт командой `\! sleep` — соединение остаётся открытым, а `psql -f` не доходит до конца файла, при котором сессия закрылась бы и транзакция откатилась.
- Блоки с `\set ON_ERROR_STOP off` показывают ошибку и идут дальше: без этого `psql` завершился бы с кодом 1.
- Данные — набор `small`. PID в страницу не вынесены: сессии названы по `application_name`. PID и время у вас будут другими, состояния и ожидания те же.
- «Сегодня» — 2026-11-20, даты в данных литералом.

## Сценарий пожара

Строка выдачи с `id = 1` в наборе `small` выглядит так.

```sql title="row-before.sql"
SELECT id, book_id, due_at, returned_at FROM loans WHERE id = 1;
```

```text title="Вывод"
 id | book_id |   due_at   | returned_at 
----+---------+------------+-------------
  1 |       4 | 2026-01-24 | 2026-01-20
(1 row)
```

Дальше четыре сессии. Первая — корень пожара: открыла транзакцию, изменила строку и не закрывает её. Так выглядит приложение, которое начало транзакцию, ушло по сетевому вызову и забыло про неё. Транзакция держит блокировку строки, пока не завершится.

```sql title="fire-holder.sql"
-- Сессия-корень: открывает транзакцию, меняет строку и молчит.
SET idle_in_transaction_session_timeout = '50s';  -- сервер закроет сессию сам
BEGIN;
UPDATE loans SET due_at = due_at + 100 WHERE id = 1;
\! sleep 55
```

Вторая хочет изменить ту же строку и ждёт чужую транзакцию.

```sql title="fire-waiter.sql"
-- Вторая сессия: хочет ту же строку и встаёт в ожидание.
SET lock_timeout = '50s';
UPDATE loans SET due_at = due_at + 1 WHERE id = 1;
```

Третья — миграция. `ALTER TABLE` берёт самую сильную блокировку на таблицу (`ACCESS EXCLUSIVE`), которая несовместима с любой другой, и встаёт в очередь. Блок завершается `ROLLBACK`, чтобы не менять схему на стенде.

```sql title="fire-alter.sql"
-- Третья сессия: миграция. ALTER TABLE ждёт блокировку, а за ним встают все.
SET lock_timeout = '50s';
BEGIN;
ALTER TABLE loans ADD COLUMN fire_note text;
ROLLBACK;
```

Четвёртая — обычный `SELECT count(*)`, который до миграции работал бы мгновенно.

```sql title="fire-reader.sql"
-- Четвёртая сессия: обычный запрос на чтение, который раньше работал.
SET statement_timeout = '50s';
SELECT count(*) FROM loans;
```

Каждый блок запускается через `make sql-bg`, и сразу после этого пожар идёт сам: ничего не завершится, пока корень не отпустит строку.

## Что выполняется сейчас

Начать с `pg_stat_activity`: одна строка на сессию. Важны `state` (`active`, `idle in transaction`, `idle`), `wait_event_type` и `wait_event` (на чём сессия стоит) и начало запроса.

```sql title="who-is-running.sql"
SELECT application_name AS session, state, wait_event_type AS wait_type, wait_event,
       left(query, 34) AS query
FROM pg_stat_activity
WHERE datname = 'library' AND pid <> pg_backend_pid()
ORDER BY backend_start;
```

```text title="Вывод"
   session   |        state        | wait_type |  wait_event   |               query                
-------------+---------------------+-----------+---------------+------------------------------------
 fire-holder | idle in transaction | Client    | ClientRead    | UPDATE loans SET due_at = due_at +
 fire-waiter | active              | Lock      | transactionid | UPDATE loans SET due_at = due_at +
 fire-alter  | active              | Lock      | relation      | ALTER TABLE loans ADD COLUMN fire_
 fire-reader | active              | Lock      | relation      | SELECT count(*) FROM loans;
(4 rows)
```

Читается так. Держатель в состоянии `idle in transaction`: запрос в колонке `query` — его последний выполненный, а ждёт он клиента (`ClientRead`). Остальные три `active`, но стоят на блокировке (`Lock`). Виды ожидания разные: `transactionid` — ждём завершения чужой транзакции из-за строки, `relation` — ждём блокировку таблицы. Длинный запрос в обычном пожаре выглядит иначе: `active` с давним `query_start` и `wait_event_type` пустым или `IO`.

## Кто кого блокирует

`pg_blocking_pids(pid)` возвращает процессы, которые не пускают сессию дальше. Колонка `pid` ради удобства чтения заменена именем сессии из `application_name`.

```sql title="who-blocks-whom.sql"
SELECT a.application_name AS session, a.state,
       (SELECT string_agg(b.application_name, ', ' ORDER BY b.backend_start)
          FROM pg_stat_activity AS b
         WHERE b.pid = ANY (pg_blocking_pids(a.pid))) AS blocked_by
FROM pg_stat_activity AS a
WHERE a.datname = 'library' AND cardinality(pg_blocking_pids(a.pid)) > 0
ORDER BY a.backend_start;
```

```text title="Вывод"
   session   | state  |        blocked_by        
-------------+--------+--------------------------
 fire-waiter | active | fire-holder
 fire-alter  | active | fire-holder, fire-waiter
 fire-reader | active | fire-alter
(3 rows)
```

Миграция ждёт и держателя, и вторую сессию: ей мешает не только блокировка таблицы от `UPDATE` держателя, но и блокировка таблицы, уже взятая второй сессией перед ожиданием строки. А читатель ждёт именно миграцию: запрос на блокировку таблицы встаёт в очередь за ждущим `ACCESS EXCLUSIVE`, хотя сама по себе слабая блокировка читателя с блокировкой держателя совместима. Одна медленная миграция за открытой транзакцией останавливает и чтение; на этом строится «почему миграции идут с `lock_timeout`».

Когда сессий много, цепочки глазами не прочесть. Рекурсивный запрос находит корень (блокирует, но сам не ждёт) и спускается вниз; если сессия ждёт по нескольким путям, она показана на ближайшем к корню уровне.

```sql title="blocking-chain.sql"
WITH RECURSIVE edges AS (
    SELECT pid AS waiter, unnest(pg_blocking_pids(pid)) AS blocker
    FROM pg_stat_activity
    WHERE datname = 'library'
), tree AS (
    SELECT blocker AS pid, 0 AS depth, ARRAY[blocker] AS path
    FROM edges
    WHERE blocker NOT IN (SELECT waiter FROM edges)   -- корень: блокирует, сам не ждёт
    UNION ALL
    SELECT e.waiter, t.depth + 1, t.path || e.waiter
    FROM edges AS e
    JOIN tree AS t ON e.blocker = t.pid
    WHERE e.waiter <> ALL (t.path)
)
SELECT repeat('  ', depth) || a.application_name AS chain, depth, a.state
FROM (SELECT DISTINCT ON (pid) pid, depth FROM tree ORDER BY pid, depth) AS t
JOIN pg_stat_activity AS a USING (pid)
ORDER BY depth, a.backend_start;
```

```text title="Вывод"
      chain      | depth |        state        
-----------------+-------+---------------------
 fire-holder     |     0 | idle in transaction
   fire-waiter   |     1 | active
   fire-alter    |     1 | active
     fire-reader |     2 | active
(4 rows)
```

Корень один: `fire-holder`, глубина 0. Его и надо разбирать. Если корня нет, а ожидающие есть, в цепочке цикл — дедлок, который сервер сам разрешает ошибкой `40P01` через `deadlock_timeout` (одна секунда по умолчанию); запрос защищён от цикла проверкой `e.waiter <> ALL (t.path)`.

## Соединения и лимит

Каждая застрявшая сессия занимает соединение. Когда их накопится столько, сколько разрешено, новые клиенты получают отказ (`FATAL: sorry, too many clients already`), и тогда падает уже всё приложение, а не одна операция. Этот отказ в прогоне не воспроизводился. Первый срез — сколько сессий в каком состоянии.

```sql title="connections-by-state.sql"
SELECT coalesce(state, 'без состояния') AS state, count(*) AS sessions
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY state
ORDER BY sessions DESC, state;
```

```text title="Вывод"
        state        | sessions 
---------------------+----------
 active              |        4
 idle in transaction |        1
(2 rows)
```

Здесь `active` — три ждущие сессии и сама диагностика, `idle in transaction` — держатель. На боевой базе тревожны два перекоса: много `idle in transaction` (приложения держат транзакции открытыми) и много `active` с одним и тем же ожиданием (все стоят в одной очереди). Второй срез — занято против лимита.

```sql title="connections-limit.sql"
SELECT count(*) AS used,
       current_setting('max_connections')::int AS max_connections,
       current_setting('superuser_reserved_connections')::int AS reserved_for_superuser,
       round(100.0 * count(*) / current_setting('max_connections')::int, 1) AS used_pct
FROM pg_stat_activity
WHERE backend_type = 'client backend';
```

```text title="Вывод"
 used | max_connections | reserved_for_superuser | used_pct 
------+-----------------+------------------------+----------
    5 |             100 |                      3 |      5.0
(1 row)
```

В этом контейнере `max_connections` равно 100, ещё 3 соединения зарезервированы для суперпользователя (`superuser_reserved_connections`), чтобы он мог зайти и разобрать пожар даже при заполненных слотах. Занято 5, то есть 5 %. Приближение к 100 — повод смотреть не только на лимит, но и на пул соединений приложения: он прячет рост числа сессий до отказа.

## Как снять

Принцип: от мягкого к жёсткому. `pg_cancel_backend(pid)` отменяет текущий запрос сессии, сама сессия и открытая транзакция остаются. `pg_terminate_backend(pid)` закрывает сессию целиком, транзакция откатывается.

Сначала отменим ожидающую сессию — вторую.

```sql title="cancel-waiter.sql"
SELECT pg_cancel_backend(pid) AS cancelled
FROM pg_stat_activity WHERE application_name = 'fire-waiter';
SELECT pg_sleep(0.5);
SELECT application_name AS session, state,
       (SELECT string_agg(b.application_name, ', ' ORDER BY b.backend_start)
          FROM pg_stat_activity AS b
         WHERE b.pid = ANY (pg_blocking_pids(a.pid))) AS blocked_by
FROM pg_stat_activity AS a
WHERE datname = 'library' AND pid <> pg_backend_pid()
ORDER BY backend_start;
```

```text title="Вывод"
 cancelled 
-----------
 t
(1 row)

 pg_sleep 
----------
 
(1 row)

   session   |        state        | blocked_by  
-------------+---------------------+-------------
 fire-holder | idle in transaction | 
 fire-alter  | active              | fire-holder
 fire-reader | active              | fire-alter
(3 rows)
```

`cancelled = t` значит, что сигнал отправлен, а не что запрос завершился; его надо проверять по `pg_stat_activity`. Вторая сессия исчезла из списка, её `UPDATE` не выполнен. Но корень на месте, а миграция и читатель стоят по-прежнему, теперь миграция ждёт одного держателя. Отмена ожидающих пожар не тушит: она убирает одного ждущего, а корень продолжает держать блокировку.

Теперь попробуем отменить запрос корня.

```sql title="cancel-holder.sql"
SELECT pg_cancel_backend(pid) AS cancelled
FROM pg_stat_activity WHERE application_name = 'fire-holder';
SELECT pg_sleep(0.5);
SELECT application_name AS session, state, left(query, 34) AS query
FROM pg_stat_activity WHERE application_name = 'fire-holder';
```

```text title="Вывод"
 cancelled 
-----------
 t
(1 row)

 pg_sleep 
----------
 
(1 row)

   session   |        state        |               query                
-------------+---------------------+------------------------------------
 fire-holder | idle in transaction | UPDATE loans SET due_at = due_at +
(1 row)
```

Сигнал отправлен (`t`), а сессия как была `idle in transaction`, так и осталась: отменять нечего, у неё нет выполняющегося запроса, она просто не завершила транзакцию. `pg_cancel_backend` на простаивающую в транзакции сессию не действует. Остаётся `pg_terminate_backend`.

```sql title="terminate-holder.sql"
SELECT pg_terminate_backend(pid) AS terminated
FROM pg_stat_activity WHERE application_name = 'fire-holder';
SELECT pg_sleep(1);
SELECT application_name AS session, state
FROM pg_stat_activity
WHERE datname = 'library' AND pid <> pg_backend_pid();
```

```text title="Вывод"
 terminated 
------------
 t
(1 row)

 pg_sleep 
----------
 
(1 row)

 session | state 
---------+-------
(0 rows)
```

Сессия закрыта, и список после паузы пуст: освободилась таблица, и миграция с читателем отработали и завершились. Что стало с транзакцией держателя, видно по данным.

```sql title="row-after.sql"
SELECT id, due_at,
       (SELECT count(*) FROM information_schema.columns
         WHERE table_name = 'loans' AND column_name = 'fire_note') AS fire_note_columns
FROM loans WHERE id = 1;
```

```text title="Вывод"
 id |   due_at   | fire_note_columns 
----+------------+-------------------
  1 | 2026-01-24 |                 0
(1 row)
```

`due_at` строки 1 прежний (2026-01-24, как в `row-before.sql`): `UPDATE ... + 100` держателя откатился вместе с сессией. Откат — главная цена `pg_terminate_backend`: всё, что приложение сделало в этой транзакции и не закоммитило, потеряно, и приложение узнает об этом по обрыву соединения. Столбца `fire_note` нет, потому что миграция в блоке завершается `ROLLBACK`. На боевой базе `pg_terminate_backend` на корне с активным длинным запросом откатывает и его работу: откат долгой записи может идти не мгновенно.

Поэтому порядок такой: найти корень, посмотреть, что он делает (`state`, `query`, `xact_start`), отменить запрос, и только когда сессия простаивает в транзакции или отмена не помогает, закрывать её. Закрывать нужно именно корень, а не очередь: её сессии разойдутся сами.

## Таймауты против пожара

Три настройки закрывают три разные дыры. Все выключены по умолчанию (ноль).

```sql title="timeout-settings.sql"
SELECT name, setting, unit
FROM pg_settings
WHERE name IN ('statement_timeout', 'lock_timeout', 'idle_in_transaction_session_timeout')
ORDER BY name;
```

```text title="Вывод"
                name                 | setting | unit 
-------------------------------------+---------+------
 idle_in_transaction_session_timeout | 0       | ms
 lock_timeout                        | 0       | ms
 statement_timeout                   | 0       | ms
(3 rows)
```

Настройки задаются на сессию (`SET`), на транзакцию (`SET LOCAL`), на роль (`ALTER ROLE app SET ...`) или в конфиге сервера. Для приложения естественное место — роль или строка подключения, для миграции — `SET LOCAL` внутри её транзакции.

### statement_timeout

Ограничивает время одного запроса, считая и ожидание блокировки. Запустим снова держателя, чтобы строка была занята.

```sql title="fire-holder-2.sql"
-- Сессия-корень: открывает транзакцию, меняет строку и молчит.
SET idle_in_transaction_session_timeout = '50s';  -- сервер закроет сессию сам
BEGIN;
UPDATE loans SET due_at = due_at + 100 WHERE id = 1;
\! sleep 55
```

```sql title="statement-timeout.sql"
\set ON_ERROR_STOP off
SET statement_timeout = '1s';
SELECT pg_sleep(3);
UPDATE loans SET due_at = due_at + 1 WHERE id = 1;
```

```text title="Вывод"
ERROR:  canceling statement due to statement timeout
ERROR:  canceling statement due to statement timeout
CONTEXT:  while updating tuple (0,1) in relation "loans"
```

Первая ошибка — обычный долгий запрос (`pg_sleep(3)` при лимите в секунду). Вторая — `UPDATE`, который застрял на занятой строке: в `CONTEXT` видно, что он ждал при обновлении кортежа `loans`. Время ожидания блокировки входит в `statement_timeout`.

### lock_timeout

Ограничивает только ожидание блокировки, а не работу запроса. Для миграций это главная настройка: она не даёт `ALTER TABLE` встать в очередь надолго и остановить чтение.

```sql title="lock-timeout.sql"
\set ON_ERROR_STOP off
SET lock_timeout = '1s';
ALTER TABLE loans ADD COLUMN fire_note text;
SELECT count(*) AS waiting_for_lock
FROM pg_stat_activity
WHERE datname = 'library' AND wait_event_type = 'Lock';
```

```text title="Вывод"
ERROR:  canceling statement due to lock timeout
 waiting_for_lock 
------------------
                0
(1 row)
```

Миграция ждала секунду и сдалась с ошибкой, и очередь за ней не образовалась: ждущих блокировку сессий ноль. Ошибку ловит раннер миграций и повторяет с паузой (подробно — [[Миграции без простоя]]), а приложение при этом читает без перебоев.

Теперь убрать держателя.

```sql title="terminate-holder-2.sql"
SELECT pg_terminate_backend(pid) AS terminated
FROM pg_stat_activity WHERE application_name = 'fire-holder-2';
SELECT pg_sleep(1);
SELECT application_name AS session, state
FROM pg_stat_activity
WHERE datname = 'library' AND pid <> pg_backend_pid();
```

```text title="Вывод"
 terminated 
------------
 t
(1 row)

 pg_sleep 
----------
 
(1 row)

 session | state 
---------+-------
(0 rows)
```

### idle_in_transaction_session_timeout

Закрывает сессию, которая слишком долго простаивает в открытой транзакции, — ровно нашего корня. Сервер закрывает её сам, без чьей-либо команды. Здесь лимит 3 секунды, чтобы не ждать.

```sql title="idle-holder.sql"
SET idle_in_transaction_session_timeout = '3s';
BEGIN;
UPDATE loans SET due_at = due_at + 100 WHERE id = 1;
\! sleep 8
```

Сразу после запуска сессия в той самой позе.

```sql title="idle-seen.sql"
SELECT application_name AS session, state, left(query, 34) AS query
FROM pg_stat_activity WHERE application_name = 'idle-holder';
```

```text title="Вывод"
   session   |        state        |               query                
-------------+---------------------+------------------------------------
 idle-holder | idle in transaction | UPDATE loans SET due_at = due_at +
(1 row)
```

Через пять секунд от запуска, когда лимит давно прошёл, она исчезла, а строка осталась прежней.

```sql title="idle-gone.sql"
SELECT (SELECT count(*) FROM pg_stat_activity
         WHERE application_name = 'idle-holder') AS sessions,
       (SELECT due_at FROM loans WHERE id = 1) AS due_at;
```

```text title="Вывод"
 sessions |   due_at   
----------+------------
        0 | 2026-01-24
(1 row)
```

Сервер отметил закрытие в своём журнале. Это единственный блок страницы не из `make sql-block`: команда `bash` запускается из корня репозитория, а `tail -1` оставляет последнюю такую строку, если до неё в контейнере уже закрывались другие сессии.

```bash title="idle-log.sh"
docker logs reference-pg 2>&1 | grep -o 'FATAL.*idle-in-transaction.*' | tail -1
```

```text title="Вывод"
FATAL:  terminating connection due to idle-in-transaction timeout
```

Транзакция откатилась: `due_at` прежний (2026-01-24), то есть и здесь изменение не сохранилось. Это дешёвая страховка от «забытой» транзакции, но не лекарство: приложение, которое так закрыли, получит обрыв соединения посреди транзакции и должно уметь её повторить. Лимит выбирают с запасом над самой долгой честной транзакцией приложения.

## Что спросят дальше

- **Запрос завис, что делать первым.** Не перезапускать базу и не убивать всё подряд: посмотреть `pg_stat_activity`, найти корень по `pg_blocking_pids`, выяснить, что он делает, отменить запрос и только потом закрывать сессию.
- **Чем `pg_cancel_backend` отличается от `pg_terminate_backend`.** Первый отменяет запрос и оставляет сессию и транзакцию (на сессию без запроса, `idle in transaction`, не действует), второй закрывает сессию, и транзакция откатывается.
- **Почему миграция остановила чтение.** `ALTER TABLE` ждёт блокировку таблицы за открытой транзакцией, а все следующие запросы встают за ним в очередь. Лекарство: `lock_timeout` и повтор в миграции, короткие транзакции в приложении.
- **Что такое `idle in transaction` и чем вредно.** Транзакция открыта, запросов нет; она держит блокировки строк, мешает автовакууму убирать мёртвые версии строк и занимает соединение. Закрывается `idle_in_transaction_session_timeout` и дисциплиной в коде.
- **Как понять, что кончились соединения.** Число сессий против `max_connections` с учётом резерва суперпользователя, разбивка по `state`; причины — утечка соединений, длинные транзакции, отсутствие пула.
- **Что делает дедлок и как он отличается от очереди.** В очереди есть корень, который не ждёт никого, в дедлоке цикл. Сервер сам убивает одного участника ошибкой `40P01`; приложение её ловит и повторяет транзакцию.
