---
title: Надёжная доставка в RabbitMQ
---

Где и как RabbitMQ теряет сообщения и что включить на продюсере, брокере и консюмере: publisher confirms, `mandatory`, durable и persistent, ручной ack, transactional outbox.
Рядом: [[RabbitMQ]] · [[Exchange и маршрутизация]] · [[Типы очередей RabbitMQ]] · [[Повторы и DLQ в RabbitMQ]] · [[Конкурентность и порядок в RabbitMQ]] · [[Когда двигать offset]]
Разделы: [[#Суть]] · [[#Публикация в никуда]] · [[#Что переживает перезапуск брокера]] · [[#Подтверждение на консюмере]] · [[#Что теряется при каждом выключенном пункте]] · [[#Transactional outbox для публикации]] · [[#Бизнес-кейс]] · [[#Что спросят дальше]]

## Суть

Сообщение может потеряться на трёх участках: при отправке, внутри брокера и при обработке. Защита нужна на каждом, потому что она работает цепочкой: сильное звено не спасает, если соседнее выключено.

- **На продюсере.** Включают publisher confirms: брокер присылает отправителю подтверждение, что принял сообщение и взял на себя ответственность за него. Без этого отправитель не знает, дошло ли сообщение. Ещё нужен флаг `mandatory` («обязательно») либо alternate exchange ([[Exchange и маршрутизация#Alternate exchange]]): без одного из них сообщение, для которого нет подходящей очереди, молча пропадает.
- **На брокере.** Очередь объявляют durable («долговечной»: она переживает перезапуск брокера), а сообщения публикуют как persistent (брокер пишет их на диск). Лучший тип очереди — **quorum queue** («кворумная»): сообщения реплицируются на несколько узлов по алгоритму консенсуса Raft, и запись считается принятой, когда её сохранило большинство ([[Типы очередей RabbitMQ#Quorum]]).
- **На консюмере.** Включают ручной ack: консюмер подтверждает сообщение только после того, как обработал его, то есть записал результат в базу. Это аналог фиксации offset в Kafka после записи в базу данных ([[Когда двигать offset]]). Если консюмер упал, не подтвердив сообщение, брокер вернёт его в очередь и отдаст другому консюмеру.

Из-за возврата сообщение может прийти повторно: это гарантия at-least-once («как минимум один раз»; возможны дубли, но не потери). Противоположная, at-most-once («не более одного раза»), допускает потери, но не дубли. Exactly-once («ровно один раз») брокер не даёт, поэтому обработчик делают идемпотентным ([[Конкурентность и порядок в RabbitMQ#Идемпотентный консьюмер]]).

## Публикация в никуда

**Задача.** Опубликовать сообщение с ключом, на который нет ни одного binding, и узнать, что оно не дошло.

Есть три режима публикации, и они ведут себя по-разному. Без confirms и без `mandatory` продюсер получает управление обратно сразу, будто всё хорошо. С confirms, но без `mandatory`, брокер честно присылает ack, потому что принял сообщение, а то, что маршрута нет, ему «не интересно». Только `mandatory` вместе с обработкой возврата (`basic.return`) даёт ошибку.

```python
import aio_pika
from aio_pika import Message
from aio_pika.exceptions import DeliveryError

conn = await aio_pika.connect_robust(URL)

# 1. без confirms и без mandatory: неправильный вариант
ch = await conn.channel(publisher_confirms=False)
ex = await ch.declare_exchange("demo.orders", "direct", durable=True)
r = await ex.publish(Message(b"1"), routing_key="no.such.key", mandatory=False)

# 2. с confirms, но без mandatory: тоже неправильный вариант
ch2 = await conn.channel(publisher_confirms=True, on_return_raises=True)
ex2 = await ch2.get_exchange("demo.orders")
r = await ex2.publish(Message(b"2"), routing_key="no.such.key", mandatory=False)

# 3. confirms + mandatory + on_return_raises: правильный вариант
try:
    await ex2.publish(Message(b"3"), routing_key="no.such.key", mandatory=True)
except DeliveryError as e:
    print(e)
```

```text title="Вывод"
1) без confirms, без mandatory   -> NoneType (исключения нет)
2) confirms, без mandatory       -> Ack (брокер подтвердил, а сообщения нет)
3) confirms + mandatory          -> DeliveryError: Message delivery failed: Basic.Return(reply_code=312, reply_text='NO_ROUTE', exchange='demo.orders', routing_key='no.such.key')
4) маршрут есть                  -> Ack
   в очереди сообщений: 1
```

Случаи 1 и 2 показывают, почему «исключений нет» не означает «доставлено»: во втором случае брокер вернул Ack на сообщение, которое никуда не попало. Только третий вариант превращает отсутствие маршрута в ошибку: брокер присылает `basic.return` с кодом 312 `NO_ROUTE`, и клиент поднимает исключение. Сам `basic.return` приходит до подтверждения, поэтому после `await publish(...)` результат уже известен.

> [!warning] Грабли aio-pika
> В aio-pika 10 метод `exchange.publish()` по умолчанию вызывается с `mandatory=True`, а возврат без `on_return_raises=True` не превращается в исключение: он только пишется в лог (`Unhandled message ... returning`), а метод возвращает объект `Basic.Return`. Поэтому «mandatory включён» ничего не гарантирует, пока канал не создан с `on_return_raises=True` либо код сам не обрабатывает `channel.return_callbacks`.

Что делать при ошибке публикации, зависит от смысла. `NO_ROUTE` — обычно ошибка конфигурации (нет очереди или binding), повтор не поможет, нужен алерт. `Nack` на confirm (например, очередь переполнена и работает `reject-publish`, см. [[Типы очередей RabbitMQ#Лимиты длины и x-overflow]]) и сетевой сбой — временные, их повторяют с паузой, но тогда возможен дубль: сообщение могло дойти, а подтверждение потерялось. Поэтому в сообщении всегда есть `message_id`.

## Что переживает перезапуск брокера

Durable-очередь переживает перезапуск, но сообщения в ней — только если они persistent (`delivery_mode=2`, в aio-pika `DeliveryMode.PERSISTENT`). Quorum queue пишет все сообщения на диск и репликам независимо от `delivery_mode`.

**Задача.** Проверить, что останется после перезапуска брокера.

```python
for name, args in [("demo.classic", {}), ("demo.quorum", {"x-queue-type": "quorum"})]:
    await ch.declare_queue(name, durable=True, arguments=args)
    for mode in (DeliveryMode.PERSISTENT, DeliveryMode.NOT_PERSISTENT):
        await ch.default_exchange.publish(
            Message(mode.name.encode(), delivery_mode=mode), routing_key=name)
# docker restart ref-rabbit, затем читаем обе очереди
```

```text title="Вывод"
до перезапуска: по 2 сообщения в classic и quorum
после перезапуска demo.classic: ['PERSISTENT']
после перезапуска demo.quorum: ['PERSISTENT', 'NOT_PERSISTENT']
```

В classic-очереди выжило только persistent-сообщение, а non-persistent исчезло. В quorum-очереди остались оба: она ведёт собственный журнал Raft, и флаг `delivery_mode` на неё не влияет. Практический вывод: classic без `delivery_mode=2` — это только для данных, которые не жалко потерять; quorum снимает этот риск сама.

> [!warning] Грабли
> Persistent без durable-очереди смысла не имеет: исчезнет сама очередь. В RabbitMQ 4.4 такую очередь вообще нельзя объявить: сообщение брокера `Feature transient_nonexcl_queues is deprecated. By default, this feature is not permitted anymore`, и он закрывает **всё соединение**, а не только канал. Остаются exclusive-очереди (живут вместе с соединением) для временных ответов.

Persistent сам по себе не говорит продюсеру, что сообщение уже принято. Подтверждение приёма даёт только publisher confirm, и для persistent-сообщения брокер присылает его после записи на диск. Поэтому persistent сочетают с confirms: без них persistent защищает от перезапуска брокера, но не от потери самого сообщения на сети.

## Подтверждение на консюмере

После доставки сообщение в брокере переходит в состояние unacked («выдано, но не подтверждено»). Дальше возможны три ответа.

- `ack` — обработано, удалить.
- `nack` (negative acknowledgement, «не смог обработать») и `reject` — отклонить. С флагом `requeue=True` сообщение возвращается в очередь, с `requeue=False` — удаляется или уходит в dead letter exchange ([[Повторы и DLQ в RabbitMQ]]). Разница между ними только в том, что `nack` умеет `multiple=True` (отклонить сразу всё по номер доставки включительно); `reject` работает с одним сообщением.
- Обрыв соединения или закрытие канала без ответа: брокер возвращает все unacked-сообщения в очередь. Они помечаются как `redelivered=True`.

Автоматическое подтверждение (`no_ack=True`, autoack) ставит сообщение «в обработанные» в момент отправки по сети, ещё до того, как консюмер его хоть раз прочитал.

**Задача.** Сравнить потери при падении консюмера: autoack против ручного ack.

```python
async def scenario(no_ack, qname):
    conn = await aio_pika.connect_robust(URL)
    ch = await conn.channel()
    q = await ch.declare_queue(qname, durable=True)
    await ch.default_exchange.publish(Message(b"charge #1", delivery_mode=2), routing_key=qname)
    got = asyncio.Event()
    async def worker(m):
        print(f"консюмер получил {m.body!r}, redelivered={m.redelivered}; начинает работу и падает")
        got.set()
        await asyncio.sleep(60)
    await q.consume(worker, no_ack=no_ack)
    await got.wait()
    await conn.close()      # «падение»: соединение оборвалось, ack не отправлен
    # ... новое соединение, q.get(): что осталось в очереди?
```

```text title="Вывод"
no_ack=True (autoack):
  консюмер получил b'charge #1', redelivered=False; начинает работу и падает
  после падения в очереди: пусто
no_ack=False (ручной ack):
  консюмер получил b'charge #1', redelivered=False; начинает работу и падает
  после падения в очереди: b'charge #1', redelivered=True
```

С autoack списание потеряно: брокер считал его доставленным в тот же миг, и вернуть его некому. При ручном ack сообщение вернулось с `redelivered=True` и достанется следующему консюмеру. Цена — возможный дубль: если консюмер успел выполнить эффект (списал деньги), но упал до `ack`, повторная доставка выполнит его второй раз. Ответ на это — идемпотентность, а не отказ от ручного ack ([[Конкурентность и порядок в RabbitMQ#Идемпотентный консьюмер]]).

В aio-pika удобно подтверждать через `async with message.process():`: при выходе без исключения сообщение подтверждается, при исключении — отклоняется (по умолчанию `requeue=False`, и ядовитое сообщение не зациклится, но и не повторится). Явные `await m.ack()` после записи в базу понятнее, чем скрытое поведение контекста, когда речь о деньгах.

> [!warning] Грабли
> `nack(requeue=True)` без паузы — горячий цикл на одном сообщении: на измерении одно «ядовитое» сообщение было доставлено более 10 000 раз за 3 секунды. Разбор и защита — в [[Повторы и DLQ в RabbitMQ#Защита от бесконечного цикла]].

## Что теряется при каждом выключенном пункте

| Что выключено | Что теряется | Когда это допустимо |
| --- | --- | --- |
| publisher confirms | сообщение, которое не дошло до брокера или не записано; продюсер об этом не знает | метрики, логи |
| `mandatory` и alternate exchange | сообщение, для которого нет очереди (опечатка в ключе, ещё не создан binding) | почти никогда |
| durable-очередь | сама очередь и всё в ней при перезапуске брокера (в 4.4 такую очередь уже не создать) | временные очереди ответов (exclusive) |
| persistent (в classic) | сообщения в очереди при перезапуске брокера | данные, которые можно восстановить |
| quorum вместо одного узла | при потере узла со classic-очередью теряется очередь целиком | не допустимо для денег и заказов |
| ручной ack | сообщение, взятое консюмером до падения | телеметрия |
| идемпотентность консюмера | при повторе эффект применяется дважды | никогда для платёжных эффектов |

## Transactional outbox для публикации

Проблема публикации из бизнес-кода в том, что база данных и брокер — две системы и общей транзакции у них нет. Вариант `commit(); publish()` теряет событие, если процесс упал между шагами. Вариант `publish(); commit()` отправляет событие о том, чего не случилось, если коммит откатился. Выход — **transactional outbox**: событие пишут в таблицу `outbox` в той же транзакции, что и изменение данных, а отдельный воркер (relay) читает таблицу и публикует в брокер. Сам паттерн не зависит от брокера (см. [[Kafka]], [[Порядок событий]] и [[Распределённые транзакции и саги]]); здесь важна специфика RabbitMQ — подтверждение публикации.

Порядок действий relay: выбрать неотправленные строки, опубликовать с `mandatory` и дождаться confirm, и только потом поставить `sent_at`. Если процесс упал между публикацией и обновлением, строка уйдёт повторно: публикация at-least-once, и консюмер обязан быть идемпотентным по `message_id`, в качестве которого берут `id` строки outbox.

**Задача.** Показать на живом брокере, что падение relay в нужный момент даёт дубль, а не потерю. Для компактности таблицы лежат в sqlite (в бою это PostgreSQL, см. ниже).

```python
def create_order(order_id):
    with db:  # одна транзакция: заказ и событие
        db.execute("INSERT INTO orders VALUES (?, 'new')", (order_id,))
        db.execute("INSERT INTO outbox VALUES (?, 'order.created', ?, NULL)", (str(uuid.uuid4()), order_id))

async def relay(ex, crash_after_publish=False):
    rows = db.execute("SELECT id, routing_key, body FROM outbox WHERE sent_at IS NULL ORDER BY rowid").fetchall()
    for eid, key, body in rows:
        await ex.publish(Message(body.encode(), message_id=eid, delivery_mode=2),
                         routing_key=key, mandatory=True)        # ждёт confirm
        if crash_after_publish:
            print(f"  публикатор упал после publish {eid[:8]}, до UPDATE sent_at"); return
        with db:
            db.execute("UPDATE outbox SET sent_at = datetime('now') WHERE id = ?", (eid,))
        print(f"  отправлено {eid[:8]} ({body}), sent_at проставлен")
```

```text title="Вывод"
запуск 1 (упадёт после первой публикации):
  публикатор упал после publish 8d245313, до UPDATE sent_at
запуск 2 (после рестарта):
  отправлено 8d245313 (A-1), sent_at проставлен
  отправлено 0184a6e1 (A-2), sent_at проставлен
доставлено в очередь (message_id: раз): {'8d245313': 2, '0184a6e1': 1}
всего сообщений в очереди: 3 при двух событиях
```

Событие `8d245313` попало в очередь дважды, `0184a6e1` один раз: потери нет, дубль есть. Он безвреден, если консюмер дедуплицирует по `message_id`.

В PostgreSQL relay забирает пачку короткой транзакцией и публикует **вне** транзакции, чтобы сетевой вызов не держал соединение и блокировки:

```sql
-- таблица и частичный индекс по неотправленным
CREATE TABLE outbox (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  routing_key text NOT NULL,
  body jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz,
  sent_at timestamptz
);
CREATE INDEX outbox_unsent ON outbox (id) WHERE sent_at IS NULL;

-- забрать пачку (короткая транзакция)
UPDATE outbox SET locked_at = now()
 WHERE id IN (SELECT id FROM outbox
               WHERE sent_at IS NULL AND (locked_at IS NULL OR locked_at < now() - interval '1 minute')
               ORDER BY id LIMIT 100
               FOR UPDATE SKIP LOCKED)
RETURNING id, routing_key, body;

-- после confirm: отдельным коротким запросом
UPDATE outbox SET sent_at = now() WHERE id = ANY(:ids);
```

`SKIP LOCKED` не гарантирует порядок: два воркера разбирают соседние пачки. Если порядок по агрегату важен, публикует один воркер на шард либо потребитель отбрасывает устаревшие версии ([[Конкурентность и порядок в RabbitMQ#Порядок по ключу]]). За возрастом самой старой неотправленной строки (`now() - min(created_at) WHERE sent_at IS NULL`) следят: рост означает, что публикация встала. Подробнее про очередь на `SKIP LOCKED` — [[Блокировки#Техника: очередь задач на SKIP LOCKED]].

> [!warning] Грабли
> Публикация внутри транзакции базы (`publish` перед `commit`) не outbox: сеть держит транзакцию, а откат уже не вернёт сообщение. Второе: `sent_at` ставят только после confirm, иначе падение между «опубликовал» и «подтвердили» теряет событие.

## Бизнес-кейс

Платёжный сервис после успешного списания должен отправить событие `payment.paid` в бухгалтерию и в уведомления. Прямая публикация из обработчика запроса теряет событие при сбое между коммитом и сетью, а бухгалтерия узнает о платеже только на сверке. С outbox запись о платеже и событие фиксируются одной транзакцией, relay доставляет событие с повтором, пока брокер не подтвердит его, а консюмеры отсеивают дубль по `message_id`. Алерты стоят на возраст старейшей неотправленной строки outbox и на размер очереди.

## Что спросят дальше

- Что включить, чтобы не потерять сообщение? Publisher confirms и `mandatory` на продюсере, durable quorum queue и persistent на брокере, ручной ack после записи результата на консюмере, плюс идемпотентность.
- Достаточно ли publisher confirms? Нет: брокер подтверждает приём, но не маршрутизацию. Для неё нужны `mandatory` или alternate exchange.
- Чем confirms отличаются от транзакций AMQP (`tx.select`)? Транзакции блокируют канал на каждый commit и сильно медленнее; confirms асинхронны и рассчитаны на пачки. В новых системах используют confirms.
- Что значит `redelivered=True`? Брокер уже пытался доставить это сообщение, возможно, консюмер успел выполнить эффект. Это подсказка, но не гарантия: отсутствие флага тоже не означает «первый раз» (например, после сбоя узла).
- Как избежать двойной обработки? Уникальный ключ в базе по `message_id` в одной транзакции с эффектом ([[Конкурентность и порядок в RabbitMQ#Идемпотентный консьюмер]]).
