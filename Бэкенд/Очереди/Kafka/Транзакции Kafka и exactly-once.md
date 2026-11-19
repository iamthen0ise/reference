---
title: Транзакции Kafka и exactly-once
---

Конспект к собеседованию по транзакциям Kafka: что такое «ровно один раз» на самом деле, как устроен цикл «прочитал, обработал, записал» с атомарной фиксацией offset, зачем нужны `transactional.id` и `isolation.level=read_committed` и где гарантия заканчивается. Выводы получены на Apache Kafka 4.1.0, `aiokafka` 0.15.0, `confluent-kafka` 2.16.2 и Postgres 17 (Python 3.13).
Рядом: [[Kafka]] · [[Когда двигать offset]] · [[Продюсер Kafka на практике]] · [[Консьюмер и группы Kafka]] · [[Распределённые транзакции и саги]] · [[PostgreSQL]]
Разделы: [[#Что обещает Kafka]] · [[#Прочитал, обработал, записал]] · [[#read_committed и откат]] · [[#Fencing зомби]] · [[#Где гарантия заканчивается]] · [[#Эксплуатация транзакций]] · [[#Бизнес-кейс]] · [[#Что спросят дальше]]

**Суть.** Exactly-once («ровно один раз») в Kafka — это не доставка по сети без дублей, а атомарность цикла «прочитать из Kafka, посчитать, записать обратно в Kafka»: запись результата и фиксация offset входа происходят в одной транзакции, и либо применяется всё, либо ничего. Состоит гарантия из трёх частей: идемпотентный продюсер (см. [[Продюсер Kafka на практике]]), транзакция с `transactional.id` и консьюмер с `isolation.level=read_committed`. Работает она только внутри Kafka: как только результат пишется во внешнюю систему (базу, HTTP, письмо), гарантия на этом эффекте кончается, и нужна идемпотентность.

## Что обещает Kafka

Транзакционный продюсер при старте регистрируется под `transactional.id` и получает эпоху (epoch). Внутри транзакции он пишет в любое число партиций и топиков, а вместе с этим добавляет в ту же транзакцию фиксацию offset консьюмера (`send_offsets_to_transaction`). Брокер, получив commit, делает видимыми все записи сразу и сдвигает offset группы. При abort записи остаются в журнале, но помечены откатанными, и консьюмеры с `read_committed` их не отдают. Поэтому повторный запуск после падения перечитает входное сообщение (его offset не сдвинулся), а результат первой неудачной попытки никто не увидит.

## Прочитал, обработал, записал

Задача: читать `tx.in`, писать в `tx.out` верхний регистр, фиксируя offset входа в той же транзакции. В `aiokafka` транзакция — контекстный менеджер: при исключении внутри блока он вызывает abort, при выходе без ошибки — commit.

```python
consumer = AIOKafkaConsumer(IN, bootstrap_servers=BOOT, group_id=GROUP, enable_auto_commit=False,
                            auto_offset_reset="earliest", isolation_level="read_committed")
producer = AIOKafkaProducer(bootstrap_servers=BOOT, transactional_id="proc-1")
await consumer.start(); await producer.start()
while batch := await consumer.getmany(timeout_ms=2000, max_records=10):
    for tp, msgs in batch.items():
        async with producer.transaction():
            for m in msgs:
                await producer.send_and_wait(OUT, m.value.upper(), key=m.key)
                if m.value.decode() == crash_on:
                    raise RuntimeError("падение посреди транзакции")        # => abort
            await producer.send_offsets_to_transaction({tp: msgs[-1].offset + 1}, GROUP)
```

Offset входа коммитится не вызовом `consumer.commit`, а внутри транзакции: именно поэтому `enable_auto_commit=False`, а ручного `commit` в цикле нет. Первый запуск «падает» на сообщении `two`, второй проходит чисто. Результат читают два консьюмера выходного топика: с `read_uncommitted` и с `read_committed`.

```text title="Вывод"
запуск 1 (падает на 'two'):
  в транзакции: one -> ONE
  в транзакции: two -> TWO
  падение посреди транзакции: транзакция откачена
  read_uncommitted видит: ['ONE', 'TWO']
  read_committed видит:   []
запуск 2 (без падения):
  в транзакции: one -> ONE
  в транзакции: two -> TWO
  в транзакции: three -> THREE
  commit транзакции, offset входа -> 3
  read_uncommitted видит: ['ONE', 'TWO', 'ONE', 'TWO', 'THREE']
  read_committed видит:   ['ONE', 'TWO', 'THREE']
```

После первого запуска выходной топик физически содержит `ONE` и `TWO`, но откатанные: без `read_committed` они видны как обычные сообщения. После второго запуска `read_uncommitted` видит дубли `ONE`, `TWO` (результат неудачной и удачной попытки), а `read_committed` — ровно по одному экземпляру. Это и есть «эффективно ровно один раз» для цепочки Kafka в Kafka. Консоль показывает то же с другой стороны: offset 0 и 1 заняты откатанными `ONE` и `TWO`, offset 2 — служебный маркер отката, и консьюмер `read_committed` начинает с offset 3.

```text title="Вывод"
$ kafka-console-consumer.sh --topic tx.out --from-beginning --isolation-level read_committed --property print.offset=true
Offset:3	ONE
Offset:4	TWO
Offset:5	THREE
```

```text title="Вывод"
$ kafka-consumer-groups.sh --describe --group tx-proc
GROUP           TOPIC           PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG             CONSUMER-ID     HOST            CLIENT-ID
tx-proc         tx.in           0          3               3               0               -               -               -
```

> [!warning] Без read_committed транзакции не работают
> Транзакционный продюсер при `isolation.level=read_uncommitted` (дефолт консьюмера) не защищает читателя вообще: откатанные сообщения читаются как обычные. Настройку нужно ставить на **каждом** консьюмере, который читает транзакционный топик, включая консольные и отладочные.

То же на `confluent-kafka`. Там `send_offsets_to_transaction` принимает метаданные группы консьюмера, и брокер может проверить, что фиксирует offset актуальный участник группы:

```python
producer = Producer({"bootstrap.servers": BOOT, "transactional.id": "cf-proc-1"})
producer.init_transactions()
...
producer.begin_transaction()
producer.produce("cf.out", msg.value().upper(), key=msg.key())
producer.send_offsets_to_transaction(
    [TopicPartition(msg.topic(), msg.partition(), msg.offset() + 1)], consumer.consumer_group_metadata())
producer.commit_transaction()
```

```text title="Вывод"
обработано one -> ONE, offset входа -> 1
обработано two -> TWO, offset входа -> 2
обработано three -> THREE, offset входа -> 3
```

В `aiokafka` `send_offsets_to_transaction` принимает только имя группы, без метаданных (поколения участника), поэтому защитой от зомби там служит `transactional.id`: его делают стабильным на входную партицию или на экземпляр, а не случайным.

## read_committed и откат

Консьюмер `read_committed` отдаёт сообщения только до **LSO** (last stable offset, последний стабильный offset): это позиция, раньше которой нет незавершённых транзакций. Следствие: открытая и зависшая транзакция блокирует чтение всего, что записано после неё, в этой партиции, пока она не завершится или не истечёт `transaction.timeout.ms` (по умолчанию 60 секунд, брокер откатывает зависшую транзакцию сам). Поэтому транзакции держат короткими: пачка, а не «вся ночная обработка», а таймаут не больше, чем нужно. Какие транзакции открыты и в каком состоянии, показывает `kafka-transactions.sh`, см. ниже.

## Fencing зомби

Зомби — старый экземпляр, который не знает, что его заменили, и продолжает писать. Kafka отсекает его эпохой: когда новый продюсер с тем же `transactional.id` вызывает инициализацию, эпоха растёт, открытая транзакция старого прерывается, а любая операция старого получает `ProducerFenced`.

```python
old = AIOKafkaProducer(bootstrap_servers=BOOT, transactional_id="proc-1"); await old.start()
await old.begin_transaction()
await old.send_and_wait("fence.demo", b"from-old-1")
new = AIOKafkaProducer(bootstrap_servers=BOOT, transactional_id="proc-1"); await new.start()   # эпоха выросла
try:
    await old.send_and_wait("fence.demo", b"from-old-2")
except KafkaError as e:
    print("старый экземпляр:", type(e).__name__)
```

```text title="Вывод"
старый экземпляр: сообщение в открытой транзакции отправлено
новый экземпляр с тем же transactional_id стартовал (эпоха выросла, старая транзакция прервана)
старый экземпляр: ProducerFenced
старый commit: ProducerFenced
Offset:2	from-new
```

Из консоли с `read_committed` виден только `from-new` (offset 2): offset 0 занимало сообщение старого экземпляра, offset 1 — служебный маркер отката. Практическое правило отсюда: `transactional.id` — стабильное имя логического обработчика (например, `<сервис>-<входная партиция>`), а не случайная строка при каждом старте: со случайной строкой зомби отсечь нечем.

## Где гарантия заканчивается

Всё сказанное выше работает, пока результат — запись в Kafka. Типичная ошибка — считать, что транзакция Kafka охватывает и внешнюю базу. Эксперимент: в той же транзакции Kafka обработчик делает запись в Postgres (выплата по событию), а процесс «падает» после коммита в базу, но до коммита транзакции Kafka. Затем запуск 2 перечитывает сообщение.

```python
async with producer.transaction():
    await producer.send_and_wait(OUT, m.value, key=m.key)
    await db.execute("INSERT INTO payouts (event_id, amount) VALUES ($1, 100)", m.value.decode())   # свой commit в Postgres
    if crash:
        raise RuntimeError("процесс упал до commit транзакции Kafka")
    await producer.send_offsets_to_transaction({tp: m.offset + 1}, GROUP)
```

```text title="Вывод"
== naive
запуск 1:
  Postgres: выплата по evt-1 закоммичена
  процесс упал до commit транзакции Kafka
запуск 2:
  Postgres: выплата по evt-1 закоммичена
строк в payouts: 2 [('evt-1', 100), ('evt-1', 100)]
```

Kafka откатила свою часть, а запись в Postgres откатить не умеет, поэтому выплата задвоилась. Лечение — идемпотентность на стороне базы: уникальный ключ `event_id` и `ON CONFLICT DO NOTHING`.

```text title="Вывод"
== safe
запуск 1:
  Postgres: выплата по evt-1 закоммичена
  процесс упал до commit транзакции Kafka
запуск 2:
  Postgres: выплата по evt-1 закоммичена
строк в payouts: 1 [('evt-1', 100)]
```

При этом вставка во второй раз формально «прошла» (запрос выполнился), но строки не добавила. Для цепочки Kafka в базу есть два рабочих решения: либо идемпотентный обработчик с уникальным ключом (и тогда транзакции Kafka не нужны вообще: хватает at-least-once плюс идемпотентность, см. [[Когда двигать offset]]), либо хранение offset в самой базе в одной транзакции с бизнес-записью. Транзакции Kafka оправданы, когда результат — другой топик Kafka (обогащение, агрегация, маршрутизация), и нужно, чтобы выходной топик не содержал ни дублей, ни частичных результатов.

## Эксплуатация транзакций

`kafka-transactions.sh` показывает активные и завершённые транзакции, их состояние и длительность, а команда `find-hanging` (поиск зависших транзакций) и `abort` помогают, когда транзакция застряла и держит LSO.

```text title="Вывод"
$ kafka-transactions.sh --bootstrap-server localhost:9092 list
TransactionalId	Coordinator	ProducerId	TransactionState
proc-1         	1          	4         	CompleteCommit
ext-1          	1          	5         	CompleteCommit

$ kafka-transactions.sh --bootstrap-server localhost:9092 describe --transactional-id proc-1
CoordinatorId	TransactionalId	ProducerId	ProducerEpoch	TransactionState	TransactionTimeoutMs	CurrentTransactionStartTimeMs	TransactionDurationMs	TopicPartitions
1            	proc-1         	4         	4            	CompleteCommit  	60000               	1795114411575                	107267               	
```

Состояние `CompleteCommit` значит, что транзакций в процессе нет; `Ongoing` долго подряд — повод искать зависшего продюсера. Команды `find-hanging` и `abort` в рецептах не запускались.

На одиночном брокере для экспериментов пришлось выставить `transaction.state.log.replication.factor=1` и `transaction.state.log.min.isr=1`; на кластере эти значения 3 и 2, иначе внутренний топик транзакций не создаётся.

## Бизнес-кейс

Сервис обогащения читает «сырые» события платежей, добавляет справочные данные и пишет в топик для аналитики. Дубли в аналитическом топике искажают суммы, частичные записи — тоже, поэтому здесь транзакции Kafka оправданы: цикл целиком внутри Kafka. Другой сервис читает те же события и начисляет бонусы в базу: транзакции Kafka ему не помогут, нужны идемпотентность по `event_id` и вставка с уникальным ключом. Вопросы к продукту: допустимо ли чуть большее время сквозной задержки (транзакционная запись добавляет подтверждения) и кто владеет `transactional.id` каждого обработчика.

## Что спросят дальше

- Что такое exactly-once в Kafka на самом деле? Идемпотентный продюсер плюс транзакция «offset входа и запись результата атомарно» плюс `read_committed` у читателя; работает внутри Kafka. Для внешних эффектов — идемпотентность получателя.
- Зачем `transactional.id` стабильный? По нему брокер узнаёт «тот же» обработчик после рестарта и отсекает зомби эпохой; случайный идентификатор этого не даёт.
- Чем опасна долгая транзакция? Она задерживает LSO, и `read_committed` читатели не видят ничего новее неё до завершения или таймаута.
- Нужны ли транзакции Kafka, если обработчик идемпотентен? Для цепочки в базу нет: at-least-once с идемпотентностью проще и достаточно. Для цепочки Kafka в Kafka — да, если нельзя терпеть ни дублей, ни частичных результатов.
