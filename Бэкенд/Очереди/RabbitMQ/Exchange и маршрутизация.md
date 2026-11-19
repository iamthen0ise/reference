---
title: Exchange и маршрутизация
---

Как RabbitMQ решает, в какие очереди попадёт сообщение: типы exchange, `*` и `#` в шаблонах, default и alternate exchange.
Рядом: [[RabbitMQ]] · [[Надёжная доставка в RabbitMQ]] · [[Типы очередей RabbitMQ]] · [[Конкурентность и порядок в RabbitMQ]] · [[Kafka]]
Разделы: [[#Суть]] · [[#Типы exchange]] · [[#Рецепт: все типы на одном стенде]] · [[#Topic]] · [[#Alternate exchange]] · [[#Бизнес-кейс]] · [[#Что спросят дальше]]

## Суть

Продюсер не знает об очередях. Он отдаёт сообщение exchange вместе с **routing key** («ключ маршрутизации», строка вроде `order.eu.paid`), а exchange сверяет ключ и заголовки сообщения с набором **binding** («привязок»), которые к нему подключены. Каждый binding связывает exchange с одной очередью (или другим exchange) и содержит ключ, шаблон либо условия на заголовки. Для каждой подходящей очереди брокер кладёт по копии сообщения, а если ни одна не подошла, сообщение по умолчанию просто выбрасывается без ошибки. Эту молчаливую потерю закрывают `mandatory` и alternate exchange (оба — ниже и в [[Надёжная доставка в RabbitMQ#Публикация в никуда]]).

Такая развязка полезна тем, что подписчиков можно добавлять и убирать без изменений в публикующем коде: достаточно создать очередь и привязать её к нужному exchange. Очередь же, напротив, принадлежит читателю: у каждой службы, которой нужна своя копия события, должна быть собственная очередь.

## Типы exchange

| Exchange | Маршрутизация | Пример |
| --- | --- | --- |
| direct | routing key совпадает с ключом binding точно | `email` и `sms` идут каждый в свою очередь воркеров |
| topic | ключ сопоставляется с шаблоном: `*` — ровно одно слово, `#` — ноль или больше слов | `order.*.paid`, `logs.#.error` |
| fanout | сообщение копируется во все привязанные очереди, ключ игнорируется | инвалидация кэшей во всех инстансах приложения |
| headers | по заголовкам сообщения; `x-match: all` требует совпадения всех заданных заголовков, `any` — хотя бы одного | редко нужен |
| default (`""`) | встроенный exchange: сообщение попадает в очередь, чьё имя равно routing key | простая очередь задач |

Слова в ключе разделяются точками: в `order.eu.paid` три слова. Дополнительно есть exchange типа `x-consistent-hash` (плагин): он раскладывает сообщения по очередям по хешу ключа, чтобы одно значение ключа всегда попадало в одну очередь; о нём — в [[Конкурентность и порядок в RabbitMQ#Порядок по ключу]].

Типы различаются ценой и гибкостью. Direct и fanout — самые простые и быстрые; topic требует разбора шаблонов, но на практике его накладные расходы малы, и поэтому он годится как универсальный выбор для доменных событий (`<домен>.<сущность>.<событие>`, как и в именах Kafka-топиков). Headers почти не нужен: то же самое выражается ключом, а ключ читать проще.

## Рецепт: все типы на одном стенде

**Задача.** Увидеть, какая очередь что получает, не гадая по документации. Для каждого типа exchange мы создаём очереди, публикуем набор ключей и вычитываем очереди через `get`.

```python
import asyncio, aio_pika
from aio_pika import ExchangeType as T, Message

async def drain(q):
    out = []
    while (m := await q.get(fail=False)):
        out.append(m.routing_key); await m.ack()
    return out

async def main():
    conn = await aio_pika.connect_robust("amqp://guest:guest@localhost:5673/")
    ch = await conn.channel()

    d = await ch.declare_exchange("demo.direct", T.DIRECT, durable=True)
    qe = await ch.declare_queue("demo.email", durable=True); await qe.bind(d, "email")
    qs = await ch.declare_queue("demo.sms", durable=True);   await qs.bind(d, "sms")
    for k in ["email", "sms", "email", "push"]:
        await d.publish(Message(b"x"), routing_key=k, mandatory=False)

    t = await ch.declare_exchange("demo.topic", T.TOPIC, durable=True)
    q1 = await ch.declare_queue("demo.paid", durable=True);   await q1.bind(t, "order.*.paid")
    q2 = await ch.declare_queue("demo.errors", durable=True); await q2.bind(t, "logs.#.error")
    q3 = await ch.declare_queue("demo.all", durable=True);    await q3.bind(t, "#")
    for k in ["order.eu.paid", "order.paid", "order.eu.west.paid",
              "logs.error", "logs.api.error", "logs.api.db.error", "logs.api.warn"]:
        await t.publish(Message(b"x"), routing_key=k)

    f = await ch.declare_exchange("demo.fanout", T.FANOUT, durable=True)
    qa = await ch.declare_queue("demo.app1", durable=True); await qa.bind(f)
    qb = await ch.declare_queue("demo.app2", durable=True); await qb.bind(f, routing_key="игнорируется")
    await f.publish(Message(b"x"), routing_key="whatever")

    h = await ch.declare_exchange("demo.headers", T.HEADERS, durable=True)
    ha = await ch.declare_queue("demo.h_all", durable=True)
    await ha.bind(h, arguments={"x-match": "all", "format": "pdf", "type": "report"})
    hy = await ch.declare_queue("demo.h_any", durable=True)
    await hy.bind(h, arguments={"x-match": "any", "format": "pdf", "type": "report"})
    for hd in [{"format": "pdf", "type": "report"}, {"format": "pdf"}, {"type": "report", "lang": "ru"}, {"format": "csv"}]:
        await h.publish(Message(b"x", headers=hd), routing_key="")
    # ... дальше drain() по каждой очереди и print
```

```text title="Вывод"
direct  email: ['email', 'email']  sms: ['sms']   (push не попал никуда)
topic order.*.paid : ['order.eu.paid']
topic logs.#.error : ['logs.error', 'logs.api.error', 'logs.api.db.error']
topic #            : 7 из 7
fanout app1: ['whatever']  app2: ['whatever']
headers all: [{'format': 'pdf', 'type': 'report'}]
headers any: [{'format': 'pdf', 'type': 'report'}, {'format': 'pdf'}, {'lang': 'ru', 'type': 'report'}]
default exchange -> demo.tasks: demo.tasks b'x'
```

Вывод показывает несколько вещей сразу. Ключ `push` не совпал ни с одним binding, поэтому сообщение пропало без следа; `mandatory=False` мы передали явно, иначе aio-pika попросил бы брокера вернуть его (см. [[Надёжная доставка в RabbitMQ#Публикация в никуда]]). В topic `order.*.paid` поймал только `order.eu.paid`: в `order.paid` слов два, а `*` обязан заменять ровно одно слово, в `order.eu.west.paid` их четыре. А `logs.#.error` поймал и `logs.error` (ноль слов в середине), и `logs.api.db.error`. Fanout игнорирует ключ в binding и в публикации. Headers в режиме `all` пропустил только сообщение с обоими заголовками, а в режиме `any` — три из четырёх.

> [!warning] Грабли
> Привязка к exchange не существует «задним числом». Очередь получает только те сообщения, которые опубликованы после создания binding. Если консюмер ещё не поднялся и очередь с binding не объявлена, ранние публикации исчезают. Поэтому очередь и binding принято объявлять с обеих сторон (идемпотентно, `declare` с теми же аргументами ничего не ломает), либо создавать их инфраструктурой заранее.

## Topic

Topic — основной exchange для доменных событий. Ключ строится от общего к частному: `<домен>.<регион или сущность>.<событие>`, а подписчик выбирает уровень детализации шаблоном.

- `order.*.paid` — оплаты по любому региону.
- `order.#` — всё про заказы, включая `order.created`.
- `#` — вообще всё (полезно для аудита и отладки).

Два соглашения экономят время. Пробелов и спецсимволов в ключе не бывает, слова разделяются точкой. Количество слов в ключе делают фиксированным, иначе `*` перестанет работать как ожидается: на примере выше `order.paid` мимо `order.*.paid`.

## Alternate exchange

Alternate exchange (AE, «запасной обменник») — exchange, куда брокер перенаправляет сообщение, не попавшее ни в одну очередь основного exchange. Он задаётся аргументом `alternate-exchange` у основного, обычно это fanout, к которому привязана одна очередь «unrouted». Так сообщения с опечаткой в ключе не пропадают, а копятся в очереди, которую можно мониторить и разбирать.

**Задача.** Не терять сообщения с неизвестным ключом.

```python
ae = await ch.declare_exchange("demo.unrouted", T.FANOUT, durable=True)
uq = await ch.declare_queue("demo.unrouted.q", durable=True); await uq.bind(ae)

main_ex = await ch.declare_exchange(
    "demo.main", T.DIRECT, durable=True,
    arguments={"alternate-exchange": "demo.unrouted"},
)
mq = await ch.declare_queue("demo.main.q", durable=True); await mq.bind(main_ex, "known")

for k in ["known", "typo"]:
    await main_ex.publish(Message(k.encode()), routing_key=k)
```

```text title="Вывод"
alternate: main.q = ['known']  unrouted.q = [b'typo']
```

Сообщение с известным ключом пришло в основную очередь, а с опечаткой оказалось в `unrouted.q`. В отличие от `mandatory`, alternate exchange работает на стороне брокера: продюсеру ничего не возвращается, и он не узнает, что маршрута не было, зато и код отправки не усложняется. Выбирают одно из двух: `mandatory` — когда продюсер сам должен реагировать на ошибку маршрутизации (HTTP-запрос вернёт 500), alternate exchange — когда достаточно сохранить сообщение для разбора.

> [!warning] Грабли
> Если у основного exchange есть и `mandatory`, и AE, а сообщение ушло в AE, оно считается маршрутизированным, и `basic.return` продюсеру не придёт. AE также должен существовать и быть привязан к очереди: иначе сообщение пропадёт уже в нём, и никто об этом не узнает.

## Бизнес-кейс

Интернет-магазин публикует событие `order.<регион>.paid`. Склад подписан на `order.*.paid`, бухгалтерия на `order.eu.paid` (только европейские заказы по своим правилам НДС), а служба аналитики — на `order.#`. Публикующий код один и не знает ни одного из подписчиков. Позже подключают антифрод: достаточно создать очередь и binding, ничего не раскатывая в сервисе заказов. Для страховки от опечаток в ключах основной exchange получает alternate exchange с очередью `unrouted`, на размер которой стоит алерт.

## Что спросят дальше

- Чем exchange отличается от очереди? Exchange ничего не хранит и только маршрутизирует; очередь хранит сообщения до подтверждения.
- Что произойдёт, если для сообщения нет подходящей очереди? Оно будет молча удалено. Защита: `mandatory` вместе с publisher confirms, либо alternate exchange.
- Как работает default exchange? Это встроенный direct exchange с пустым именем; у каждой очереди автоматически есть binding на него с ключом, равным имени очереди. Поэтому публикация в `""` с ключом `queue_name` попадает прямо в очередь.
- Чем `*` отличается от `#`? `*` — ровно одно слово, `#` — ноль или больше слов.
- Зачем headers exchange, если есть topic? Он нужен, когда маршрутизировать надо по нескольким независимым признакам без жёсткой иерархии ключа; на практике почти всегда хватает topic.
- Как гарантировать порядок по ключу при нескольких консюмерах? См. consistent hash exchange в [[Конкурентность и порядок в RabbitMQ#Порядок по ключу]].
