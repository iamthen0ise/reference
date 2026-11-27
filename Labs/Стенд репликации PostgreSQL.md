---
title: Стенд репликации PostgreSQL
---

Лаба к разделу [[Эксплуатация PostgreSQL#Репликация]]: его утверждения можно не принимать на веру, а проверить самому. Стенд лежит в этом репозитории, в разделе [[Labs]]: PostgreSQL 17, основной сервер (primary) и три потоковые реплики со слотами, фоновая нагрузка pgbench и HTML-дашборд лага в реальном времени. Ниже — как его скачать и поднять, и пять опытов, каждый из которых подтверждает конкретное утверждение. Вывод в опытах снят на этом стенде (Docker Desktop на macOS); числа у вас будут другими, а поведение — тем же.

Рядом: [[Labs]] · [[Эксплуатация PostgreSQL]] · [[Репликация и кластер Redis]] · [[Сигналы в Linux]] · [[Docker на практике]]

Разделы: [[#Скачать и поднять]] · [[#Что внутри]] · [[#Асинхронная реплика отстаёт]] · [[#Read-your-writes и LSN]] · [[#Цена синхронной реплики]] · [[#Слот держит WAL]] · [[#Обрыв связи]] · [[#Убрать за собой]]

## Скачать и поднять

Нужны только git и Docker (Docker Desktop или Docker Engine с Compose v2).

```sh
git clone https://github.com/iamthen0ise/reference.git
cd reference
make pglab-up          # то же: cd Labs/pg-replication-lab && docker compose up -d --build
open http://localhost:8088
```

Код стенда: [`Labs/pg-replication-lab`](https://github.com/iamthen0ise/reference/tree/main/Labs/pg-replication-lab), подробности — в его `README.md`. На Linux кнопкам управления контейнерами нужен GID группы сокета Docker: `DOCKER_GID=$(stat -c %g /var/run/docker.sock) make pglab-up`.

Все команды ниже выполняются из каталога `Labs/pg-replication-lab`. Пароль и порты — только для локального стенда, порты слушают 127.0.0.1.

## Что внутри

| Сервис | Порт на хосте | Что это |
| --- | --- | --- |
| primary | 15432 | основной сервер, `postgres` / `postgres`, база `lab` |
| replica1, replica2 | 15433, 15434 | обычные асинхронные реплики |
| replica3 | 15435 | отложенная реплика: `recovery_min_apply_delay = 5s`, изображает отстающую |
| loader | — | pgbench, около 300 транзакций в секунду |
| netem | — | задержка, потери и обрыв связи primary ↔ реплика (`tc` и `iptables`) |
| dashboard | 8088 | графики лага, кнопки stop / kill / start / restart, обрыв связи, синхронная реплика, залп записи |

Почти всё ниже можно сделать кнопками дашборда; здесь показаны команды, чтобы был виден сам SQL.

## Асинхронная реплика отстаёт

**Утверждение** ([[Эксплуатация PostgreSQL#Репликация]]). Асинхронная реплика получает изменения с задержкой, основной сервер её не ждёт.

```sh
docker compose exec primary psql -U postgres -d lab -c "
  SELECT application_name, state, sync_state,
         pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)) AS behind,
         replay_lag
  FROM pg_stat_replication ORDER BY 1;"
```

```text
 application_name |   state   | sync_state | behind  |   replay_lag
------------------+-----------+------------+---------+-----------------
 replica1         | streaming | async      | 25 kB   | 00:00:00.014444
 replica2         | streaming | async      | 25 kB   | 00:00:00.009757
 replica3         | streaming | async      | 5097 kB | 00:00:04.97617
```

Обычные реплики отстают на миллисекунды и десятки килобайт WAL, отложенная — на свои 5 секунд. На дашборде тот же лаг виден сквозным замером: он 10 раз в секунду пишет время в таблицу `heartbeat` на primary и читает его на каждой реплике.

## Read-your-writes и LSN

**Утверждение** ([[Эксплуатация PostgreSQL#Репликация]], бизнес-кейс). Запись ушла на основной сервер, а чтение с асинхронной реплики ещё возвращает старые данные. Точный способ это обойти — запомнить LSN своей записи и читать с реплики, только когда она применила журнал до этой позиции.

Пользователь меняет имя с «Аня» на «Анна»; приложение запоминает LSN после коммита и сразу читает с отстающей replica3:

```sh
docker compose exec primary psql -U postgres -d lab -c "
  CREATE TABLE IF NOT EXISTS profile(id int PRIMARY KEY, name text);
  INSERT INTO profile VALUES (1, 'Аня') ON CONFLICT (id) DO UPDATE SET name = 'Аня';"
sleep 7   # пусть replica3 догонит
docker compose exec primary psql -U postgres -d lab -c "UPDATE profile SET name = 'Анна' WHERE id = 1"
LSN=$(docker compose exec -T primary psql -U postgres -d lab -Atc "SELECT pg_current_wal_lsn()")
docker compose exec replica3 psql -U postgres -d lab -c "
  SELECT name, pg_last_wal_replay_lsn() >= '$LSN' AS caught_up FROM profile WHERE id = 1;"
```

```text
 name | caught_up
------+-----------
 Аня  | f
```

Через 6 секунд тот же запрос:

```text
 name | caught_up
------+-----------
 Анна | t
```

Пока `caught_up = f`, реплика возвращает старое имя, и приложение должно читать с основного сервера. Проверка `pg_last_wal_replay_lsn() >= LSN` дешёвая, её делают прямо перед чтением.

## Цена синхронной реплики

**Утверждение** ([[Эксплуатация PostgreSQL#Репликация]]). Синхронная репликация даёт RPO = 0 ценой задержки каждой записи: `COMMIT` ждёт подтверждения от реплики.

Задержка коммита до и после включения синхронной реплики (pgbench в одно соединение, 5 секунд):

```sh
docker compose exec primary pgbench -U postgres -n -c 1 -T 5 -b simple-update lab
docker compose exec primary psql -U postgres -d lab \
  -c "ALTER SYSTEM SET synchronous_standby_names = 'FIRST 1 (replica1)'" -c "SELECT pg_reload_conf()"
docker compose exec primary pgbench -U postgres -n -c 1 -T 5 -b simple-update lab
```

| Режим | `latency average` | TPS |
| --- | --- | --- |
| все реплики асинхронные | 20,3 мс | 49 |
| replica1 синхронная | 37,9 мс | 26 |

В `pg_stat_replication` у replica1 теперь `sync_state = sync`. Каждый коммит стал почти вдвое дольше: к записи на диск добавился круг до реплики и её запись (почему запись на диск сама по себе дорогая — [[Ввод-вывод и сеть#write и fsync]]). Сама асинхронная задержка в 20 мс — это fsync тома в виртуальной машине Docker Desktop под фоновой нагрузкой; на обычном сервере она меньше, а разница между режимами остаётся.

**Обратная сторона: синхронная реплика упала.** Остановим её и попробуем записать, ограничив запрос тремя секундами:

```sh
docker compose stop replica1
docker compose exec primary psql -U postgres -d lab \
  -c "SET statement_timeout = '3s'" -c "INSERT INTO profile VALUES (2, 'Борис')"
```

Команда не завершилась ни через 3 секунды, ни через минуту: `statement_timeout` ожидание реплики не прерывает. Встала вся запись на primary, и pgbench, и heartbeat дашборда:

```sql
SELECT pid, wait_event, now() - query_start AS waiting, left(query, 40) AS query
FROM pg_stat_activity WHERE wait_event = 'SyncRep';
```

```text
 pid  | wait_event |     waiting     |                  query
------+------------+-----------------+------------------------------------------
 3218 | SyncRep    | 00:01:05.229918 | update heartbeat set ts = clock_timestam
  792 | SyncRep    | 00:01:05.272298 | END;
  793 | SyncRep    | 00:01:05.262709 | INSERT INTO events (account_id, payload)
  794 | SyncRep    | 00:01:05.261199 | END;
  795 | SyncRep    | 00:01:05.262034 | END;
 4873 | SyncRep    | 00:01:04.118468 | INSERT INTO profile VALUES (2, 'Борис')
```

> [!warning] Одна синхронная реплика — точка отказа
> С `FIRST 1 (replica1)` основной сервер не примет ни одного коммита, пока replica1 лежит. Поэтому синхронных кандидатов делают несколько (`ANY 1 (replica1, replica2)`) и следят за ними. В Redis тот же выбор решают `WAIT` и `min-replicas-to-write`: [[Репликация и кластер Redis#Рецепт: WAIT и min-replicas-to-write]].

Запись отпускает снятие требования синхронности; после этого строка «Борис» на месте:

```sh
docker compose exec primary psql -U postgres -d lab \
  -c "ALTER SYSTEM RESET synchronous_standby_names" -c "SELECT pg_reload_conf()"
docker compose start replica1
```

## Слот держит WAL

**Утверждение** ([[Эксплуатация PostgreSQL#VACUUM и раздувание таблиц (bloat)]]). Слот репликации копит WAL для отключённой реплики; забытый слот раздувает диск основного сервера (и мешает вакууму, если реплика присылала `hot_standby_feedback`).

```sh
docker compose stop replica2
docker compose exec primary psql -U postgres -d lab -c "
  SELECT slot_name, active,
         pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
  FROM pg_replication_slots WHERE slot_name = 'replica2_slot';"
```

| Момент | `active` | `retained` |
| --- | --- | --- |
| сразу после остановки | f | 1263 kB |
| через 30 секунд | f | 22 MB |
| через 15 секунд после `docker compose start replica2` | t | 25 kB |

На фоновой нагрузке слот держит около 40 МБ WAL в минуту. Вернувшаяся реплика дочитывает его по слоту и догоняет; без слота основной сервер мог бы уже удалить нужные сегменты, и реплику пришлось бы пересоздавать заново. Кнопка `kill` на дашборде даёт то же самое, только реплика умирает по SIGKILL и остаётся в `Exited (137)`: что значит код 137 — [[Сигналы в Linux#Коды выхода 128 + N]], почему `restart: unless-stopped` её не поднимает — [[Docker на практике#Перезапуск контейнера]].

## Обрыв связи

**Утверждение** ([[Эксплуатация PostgreSQL#Репликация]]). Реплика может быть жива, но отрезана от основного сервера; тогда основной сервер замечает это только по таймауту и продолжает копить WAL в слоте.

Кнопка «обрыв связи» на карточке реплики ставит `iptables DROP` в обе стороны между primary и репликой. То же через API дашборда:

```sh
curl -X POST -H 'X-Lab-Request: 1' -H 'Content-Type: application/json' \
  -d '{"enabled": true}' localhost:8088/api/replicas/replica1/partition
```

| Момент | replica1 в `pg_stat_replication` | слот `replica1_slot` |
| --- | --- | --- |
| через 3 секунды | `streaming` | активен |
| через 15 секунд | строки нет | `active = f` |
| через 8 секунд после `{"enabled": false}` | `streaming` | активен |

Первые секунды основной сервер считает реплику живой: пакеты просто не доходят. На стенде `wal_sender_timeout = 10s`, и после него walsender закрывается. Лаг на дашборде всё это время растёт линейно. После восстановления связи реплика переподключается сама и догоняет по слоту.

## Убрать за собой

```sh
make pglab-down        # то же: cd Labs/pg-replication-lab && docker compose down -v
```

Команда удаляет контейнеры и тома с данными; следующий `make pglab-up` создаст базу заново.
