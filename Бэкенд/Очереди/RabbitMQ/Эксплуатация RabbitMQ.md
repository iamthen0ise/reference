---
title: Эксплуатация RabbitMQ
---

Как смотреть, что творится в брокере: `rabbitmqctl`, `rabbitmq-diagnostics`, `rabbitmqadmin`, management API, метрики, алармы и flow control, соединения и каналы, таймаут подтверждения.
Рядом: [[RabbitMQ]] · [[Типы очередей RabbitMQ]] · [[Надёжная доставка в RabbitMQ]] · [[Конкурентность и порядок в RabbitMQ]] · [[Повторы и DLQ в RabbitMQ]]
Разделы: [[#Суть]] · [[#Консольные утилиты]] · [[#Management API и метрики]] · [[#Алармы и flow control]] · [[#Соединения и каналы]] · [[#Таймаут подтверждения]] · [[#Что мониторить]] · [[#Бизнес-кейс]] · [[#Что спросят дальше]]

## Суть

Администрирование брокера складывается из трёх задач. Нужно видеть состояние (очереди, консюмеры, соединения), понимать причину, когда что-то встало (алармы, длинные очереди, «зависшие» unacked), и вовремя замечать проблемы по метрикам, пока их не заметили пользователи. Инструменты для первых двух — консольные утилиты и management API; для третьей — метрики Prometheus и алерты. Все примеры ниже сняты на RabbitMQ 4.4.1 в Docker (`docker exec ref-rabbit ...`).

## Консольные утилиты

В поставке три утилиты с разной ролью. `rabbitmqctl` управляет узлом и показывает объекты (очереди, соединения, каналы, консюмеры). `rabbitmq-diagnostics` отвечает за диагностику и проверки здоровья. `rabbitmqadmin` (версии 2) работает через HTTP API management и умеет объявлять и удалять объекты, публиковать и читать сообщения. Глагол пишут первым словом после имени утилиты, как и в других правилах доступа.

**Задача.** Поднять тестовое состояние и осмотреть его: очередь с лимитом и консюмер, который берёт сообщения и не подтверждает.

```python
conn = await aio_pika.connect_robust(URL)
ch = await conn.channel(); await ch.set_qos(prefetch_count=5)
ex = await ch.declare_exchange("orders", "topic", durable=True)
q = await ch.declare_queue("orders.billing", durable=True,
        arguments={"x-queue-type": "quorum", "x-max-length": 10000, "x-overflow": "reject-publish"})
await q.bind(ex, "order.*.paid")
for i in range(12):
    await ex.publish(Message(f"o{i}".encode(), delivery_mode=2), routing_key="order.eu.paid")
held = []
async def h(m): held.append(m)       # берём и не подтверждаем
await q.consume(h)
```

```bash
rabbitmqctl list_queues name type messages_ready messages_unacknowledged consumers
rabbitmqctl list_exchanges name type durable
rabbitmqctl list_bindings source_name destination_name routing_key
rabbitmqctl list_connections name user state channels
rabbitmqctl list_channels connection number prefetch_count messages_unacknowledged
rabbitmqctl list_consumers
```

```text title="Вывод"
name	type	messages_ready	messages_unacknowledged	consumers
orders.billing	quorum	7	5	1

name	type	durable
	direct	true
orders	topic	true

	orders.billing	orders.billing
orders	orders.billing	order.*.paid

name	user	state	channels
192.168.65.1:65187 -> 172.17.0.3:5672	guest	running	1

connection	number	prefetch_count	messages_unacknowledged
<rabbit@cb4c1ff40039.1795113563.2566.0>	1	5	5

queue_name	channel_pid	consumer_tag	ack_required	prefetch_count	active	arguments
orders.billing	<rabbit@cb4c1ff40039.1795113563.2574.0>	ctag1.404796c3b92b4918b6930b27f2b6b78b	true	5	true	[]
```

Двенадцать сообщений распределились так: пять выданы консюмеру и ждут подтверждения (`messages_unacknowledged`, ровно по размеру prefetch), семь ждут своей очереди (`messages_ready`). Именно в такой картине видно «застрявшего» консюмера: unacked равно prefetch и не меняется. Строка без имени в `list_exchanges` — default exchange.

`rabbitmqadmin` удобен для разовых действий и скриптов:

```bash
rabbitmqadmin declare queue --name ops.test --type quorum
rabbitmqadmin publish message --routing-key ops.test --payload hello
rabbitmqadmin get messages --queue ops.test --ack-mode reject_requeue_true --count 1
rabbitmqadmin delete queue --name ops.test
```

```text title="Вывод"
Message published and routed successfully
╭───────────────┬─────────────┬──────────┬─────────────┬───────────────┬────────────┬─────────┬──────────────────╮
│ payload_bytes │ redelivered │ exchange │ routing_key │ message_count │ properties │ payload │ payload_encoding │
├───────────────┼─────────────┼──────────┼─────────────┼───────────────┼────────────┼─────────┼──────────────────┤
│ 5             │ false       │          │ ops.test    │ 0             │            │ hello   │ string           │
╰───────────────┴─────────────┴──────────┴─────────────┴───────────────┴────────────┴─────────┴──────────────────╯
```

Чтение с `reject_requeue_true` не уносит сообщение из очереди: оно просматривается и возвращается. Для боевых очередей этим пользуются осторожно: чтение через `get` не предназначено для рабочих нагрузок (в документации утилиты оно разрешено «только для разработки и тестов»).

`rabbitmq-diagnostics` — для проверок, пригодных для скриптов и проб:

```bash
rabbitmq-diagnostics check_running
rabbitmq-diagnostics alarms
rabbitmq-diagnostics memory_breakdown --unit MB
```

```text title="Вывод"
RabbitMQ on node rabbit@cb4c1ff40039 is fully booted and running
Node rabbit@cb4c1ff40039 reported no alarms, local or clusterwide
reserved_unallocated: 174.1947 mb (68.69%)
other_system: 26.3259 mb (10.38%)
code: 22.3541 mb (8.82%)
```

Вывод `alarms` и `memory_breakdown` сокращён до начала. Не путайте `alarms` с `check_local_alarms`: в 4.4 вторая команда объявлена устаревшей (no-op) и сообщает «no local alarms» при действующем аларме, поэтому для проверки пользуются `rabbitmq-diagnostics alarms`.

> [!warning] Грабли
> Все эти команды ходят к узлу по cookie Erlang, и права на них должны быть только у администраторов. В Kubernetes `rabbitmqctl` через `exec` в рабочий под — запись в боевой брокер: без явного согласия владельца кластера используйте только команды на чтение (`list_*`, `status`, `alarms`, `check_*`).

## Management API и метрики

Плагин `rabbitmq_management` (порт 15672) отдаёт тот же набор данных по HTTP, и именно на нём работают веб-интерфейс и `rabbitmqadmin`. Это удобно для скриптов, проверок готовности и сбора данных в нестандартные системы.

```python
o = await api("/overview")
q = await api("/queues/%2F/demo.c")           # %2F — виртуальный хост "/"
```

```text title="Вывод"
версия: 4.4.1 | erlang: 29.1.2 | node: rabbit…
объекты: {'channels': 0, 'consumers': 0, 'exchanges': 8, 'queues': 1, 'connections': 0}
очередь demo.c: {'type': 'quorum', 'state': 'running', 'messages_ready': 200, 'messages_unacknowledged': 0, 'consumers': 0}
message_stats: 200 publish
alarm в /health: {'status': 'ok'}
virtual hosts: ['/']
```

Адреса `/api/health/checks/...` годятся для проб Kubernetes и мониторинга (в примере `alarms`). Опрос management API для постоянного мониторинга — не лучшая идея: он считает статистику по всем объектам и сам нагружает брокер. Для этого есть плагин `rabbitmq_prometheus` (в образе включён по умолчанию, порт 15692, адрес `/metrics`): он отдаёт метрики в формате Prometheus, а на сборку стоят готовые дашборды Grafana. Вывод `/metrics` на стенде не снимался (порт не публиковался), поэтому ниже перечислены только смысл метрик, а не значения.

## Алармы и flow control

Брокер защищает себя двумя ресурсными аларами. **Memory alarm** срабатывает, когда расход памяти превышает порог (`vm_memory_high_watermark`, по умолчанию доля от оперативной памяти). **Disk alarm** срабатывает, когда на диске остаётся меньше `disk_free_limit`. При любом из них брокер **блокирует соединения, которые публикуют сообщения**, на всех узлах кластера, пока ресурс не освободится: читать консюмеры могут и дальше, и именно они возвращают ресурс. Это и есть **flow control** («управление потоком»): брокер не принимает новое, пока не разберёт накопленное.

Есть и второй, мягкий механизм: внутренний credit flow притормаживает отдельного быстрого продюсера, когда медленный консюмер или диск не успевают. Он включается постоянно и виден как соединение в состоянии `flow` в `list_connections`.

**Задача.** Увидеть, что происходит с продюсером при disk alarm. Лимит свободного места поднимаем выше реального, чтобы вызвать аларм.

```bash
rabbitmqctl set_disk_free_limit 900GB
rabbitmq-diagnostics alarms
rabbitmqctl list_connections name state
rabbitmqctl set_disk_free_limit 50MB      # вернуть
```

```python
try:
    await asyncio.wait_for(ch.default_exchange.publish(Message(b"y"), routing_key="demo.alarm"), 3)
except asyncio.TimeoutError:
    print("publish завис, таймаут")
```

```text title="Вывод"
publish до алармов: ok
Setting disk free limit on rabbit@cb4c1ff40039 to 900GB bytes ...
Asking node rabbit@cb4c1ff40039 to report any known resource alarms ...
Free disk space alarm on node rabbit@cb4c1ff40039
Listing connections ...
name	state
192.168.65.1:22891 -> 172.17.0.3:5672	blocking
publish завис, таймаут через 3 с (соединение blocked)
Setting disk free limit on rabbit@cb4c1ff40039 to 50MB bytes ...
Asking node rabbit@cb4c1ff40039 to report any known resource alarms ...
Node rabbit@cb4c1ff40039 reported no alarms, local or clusterwide
```

Публикация до аларма прошла, после него зависла: соединение перешло в состояние `blocking`, и продюсер ждёт, пока аларм не снимут. При снятии лимита брокер сообщил, что аларм исчез, и соединение разблокировалось. aio-pika при этом пишет в лог «blocked by: low on disk» и «unblocked». Что делать продюсеру: ставить таймаут на публикацию (как в примере), отслеживать события блокировки (`connection.blocked`) и не складывать запросы пользователей в память, пока брокер не отвечает. Для критичных сообщений публикуют через outbox ([[Надёжная доставка в RabbitMQ#Transactional outbox для публикации]]): блокировка брокера не должна ронять бизнес-операцию.

> [!warning] Грабли
> Пороги, выставленные командами `set_disk_free_limit` и `set_vm_memory_high_watermark`, действуют только до перезапуска узла: постоянные значения задают в конфигурации. Ещё одна ловушка: блокируются **все** публикующие соединения, даже те, которые пишут в совсем другие очереди. Изоляцию даёт только отдельный брокер или кластер. Не проверяйте аларм командой `check_local_alarms`: она в 4.4 объявлена устаревшей, печатает «DEPRECATED and is a no-op» и сообщает об отсутствии аларма, когда он действует; нужна `rabbitmq-diagnostics alarms`.

Защита от алармов — проектная. Ограничивают длину очередей (`x-max-length` и `reject-publish`, [[Типы очередей RabbitMQ#Лимиты длины и x-overflow]]), мониторят глубину очередей раньше, чем сработает аларм, и держат запас диска под рост.

## Соединения и каналы

**Соединение** (connection) — одно TCP-соединение с брокером. Создание дорогое: рукопожатие TCP, при необходимости TLS и AMQP-аутентификация. **Канал** (channel) — лёгкий виртуальный канал внутри соединения: публикации, подтверждения, консюмеры и `qos` работают на уровне канала. Каналов в соединении может быть много. Поэтому соединение держат одно на процесс, а каналы создают по потребности: обычно один канал на поток публикации и отдельный на каждого консюмера.

**Задача.** Измерить цену неправильного подхода и увидеть, как ведут себя ошибки.

```python
# антипаттерн: соединение на каждую публикацию
for i in range(N):
    c = await aio_pika.connect(URL); ch = await c.channel()
    await ch.default_exchange.publish(Message(b"x", delivery_mode=2), routing_key="demo.c"); await c.close()

# правильно: одно соединение и один канал
c = await aio_pika.connect_robust(URL); ch = await c.channel()
for i in range(N):
    await ch.default_exchange.publish(Message(b"x", delivery_mode=2), routing_key="demo.c")

chans = [await c.channel() for _ in range(5)]          # ещё пять каналов в том же соединении
bad = await c.channel()
await bad.get_exchange("no.such.exchange")             # ошибка на канале
```

```text title="Вывод"
100 публикаций, соединение на каждую : 2.59 с
100 публикаций, один канал            : 1.04 с
state	channels
running	6
ошибка на канале: ChannelNotFoundEntity | NOT_FOUND - no exchange 'no.such.exchange' in vhost '/'
канал закрыт: True | соединение закрыто: False
```

Соединение на каждую публикацию втрое медленнее даже на localhost без TLS, а на реальной сети и с TLS разница больше; кроме того, брокер тратит ресурсы на постоянное создание и закрытие соединений. Шесть каналов живут в одном соединении (`channels 6`). Ошибка в одном канале (обращение к несуществующему exchange) закрыла только его: соединение и остальные каналы работают. Так бывает не всегда: некоторые ошибки закрывают всё соединение (например, запрет на создание non-durable неэксклюзивной очереди в 4.4, [[Типы очередей RabbitMQ#Classic]]).

Практические правила:

- одно соединение на процесс и роль (отдельное для публикации и для потребления: блокировка по алармам действует на публикующие соединения, и читающее остаётся живым);
- канал не делят между потоками и задачами, которые публикуют параллельно: confirm-ы перепутаются;
- число каналов следует ограничивать: каждый канал — процесс в брокере, а большое число открытых каналов — частая причина утечек;
- у соединения задают имя (`client_properties={"connection_name": ...}`), иначе в списке видны только адреса.

## Таймаут подтверждения

**Consumer acknowledgement timeout** (`consumer_timeout`) — время, в течение которого консюмер обязан подтвердить выданное сообщение. По умолчанию 30 минут. Если ack не пришёл, брокер закрывает канал с ошибкой `PRECONDITION_FAILED`, а неподтверждённые сообщения возвращаются в очередь и достаются другому консюмеру. Проверка выполняется периодически (раз в минуту), поэтому срабатывает с запаздыванием.

Эта особенность ломает долгие задачи: работа длится 40 минут, на 30-й брокер закрыл канал, задачу получил второй консюмер, и она выполняется дважды. Решения: поднять таймаут на узле (параметр `consumer_timeout`) или у очереди, подтверждать сообщение раньше, сохраняя прогресс и идентификатор задачи в базе, либо брать сообщение, ставить задачу в отдельную рабочую очередь и сразу подтверждать.

> [!warning] Не проверено запуском
> Поведение закрытия канала по таймауту на стенде воспроизвести не удалось: очередь с аргументом `x-consumer-timeout: 2000` не закрыла канал за 150 секунд наблюдения. Значение по умолчанию (30 минут), код ошибки и периодичность проверки приведены по документации RabbitMQ. Если вы полагаетесь на таймаут, проверьте его на своей версии.

## Что мониторить

Метрики и алерты, которые важны для очередей. Конкретные пороги зависят от нагрузки; значения на стенде не снимались.

- Глубина очереди (`messages_ready`) и скорость её роста: раньше всего показывает, что консюмеры не успевают.
- Неподтверждённые сообщения (`messages_unacknowledged`) и возраст самого старого: «застрявший» консюмер видно по неизменному числу, равному prefetch.
- Число консюмеров на очереди: ноль при растущей очереди — консюмеры упали.
- Размер DLQ и parking lot: любое непустое состояние требует разбора ([[Повторы и DLQ в RabbitMQ#Parking lot]]).
- Возраст сообщения при обработке и число повторов.
- Блокировка соединений и алармы по памяти и диску.
- Скорость публикации, подтверждений и возвратов (`basic.return`) на брокере.
- Возраст самой старой неотправленной строки outbox на стороне приложения.

## Бизнес-кейс

В пятницу вечером очередь `invoices` растёт, пользователи жалуются, что счета не приходят. Дежурный смотрит `list_queues`: `messages_ready` увеличивается, консюмеров 0. `list_connections` показывает, что соединения воркеров исчезли после выкладки, а контейнеры воркеров перезапускаются. Причина — обновлённая библиотека не подключается к брокеру с новым паролем. Воркеры чинят, очередь за час разбирается. Чтобы ситуация не повторилась молча, настраивают алерт «консюмеров на `invoices` ноль дольше 2 минут» и «`messages_ready` растёт 10 минут подряд», а длину очереди ограничивают `reject-publish`, чтобы переполнение не заблокировало весь брокер.

## Что спросят дальше

- Что будет, если консюмеры не успевают? Очередь растёт, срабатывает memory или disk alarm, и брокер блокирует публикующие соединения (flow control). Защита: `x-max-length` с `reject-publish`, мониторинг глубины очереди.
- Чем соединение отличается от канала? Соединение — TCP, дорогое; канал — лёгкий виртуальный канал внутри соединения. Соединение одно на процесс, каналов много.
- Как проверить здоровье брокера из пробы? `rabbitmq-diagnostics check_running`, `alarms` или `/api/health/checks/alarms` в management API.
- Почему долгая задача «вернулась» в очередь? Сработал `consumer_timeout` (30 минут по умолчанию): нет ack, брокер закрыл канал и вернул сообщение.
- Как увидеть застрявшего консюмера? `list_channels` или `list_consumers`: число unacked равно prefetch и не меняется.
- Как отличить аларм от обычной нагрузки? Соединения публикующих клиентов в состоянии `blocking` или `blocked`, `rabbitmq-diagnostics alarms` называет ресурс.
