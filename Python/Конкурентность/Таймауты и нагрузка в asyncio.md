---
title: "Python: таймауты и нагрузка в asyncio"
---

Таймауты HTTP-клиента по фазам, ограничение параллелизма на десятках тысяч задач и блокирующий код, который останавливает цикл событий.
Рядом: [[asyncio на практике]] · [[Ошибки и отмена в asyncio]] · [[asyncio под капотом]] · [[Потоки и процессы]]
Разделы: [[#Таймауты HTTP-клиента: на фазу, а не на запрос]] · [[#Ограничение параллелизма на 100 000 задач]] · Блокирующий код в async def останавливает цикл · [[#Что спросят дальше]]

## Таймауты HTTP-клиента: на фазу, а не на запрос

`httpx.Timeout(3)` задаёт одно число четырём таймаутам сразу: на подключение (connect), чтение (read), запись (write) и ожидание соединения из пула (pool). Каждый срабатывает независимо и отсчитывается заново, поэтому «таймаут 3 секунды» не означает, что запрос уложится в 3 секунды: сервер, который присылает по байту раз в полсекунды, держит клиента сколько угодно, ни один `read` не истекает. Общий дедлайн попытки задаёт `asyncio.timeout`.

Локальный медленный сервер: после заголовков шлёт по байту каждые 0,3 с (10 байт, итого 3 с). Второй сервер принял запрос и молчит.

```python
import asyncio
import time

import httpx


async def slow(reader, writer):
    await reader.readuntil(b"\r\n\r\n")
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n")
    await writer.drain()
    for _ in range(10):          # байт за байтом, пауза 0,3 с
        await asyncio.sleep(0.3)
        writer.write(b"x")
        await writer.drain()
    writer.close()


async def silent(reader, writer):
    await reader.readuntil(b"\r\n\r\n")
    await asyncio.sleep(5)       # принял запрос и молчит


async def main() -> None:
    srv = await asyncio.start_server(slow, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{srv.sockets[0].getsockname()[1]}/"
    async with httpx.AsyncClient(timeout=httpx.Timeout(1)) as client:
        start = time.perf_counter()
        r = await client.get(url)
        print(f"Timeout(1): ответ {r.status_code} за {time.perf_counter() - start:.1f} с")

        start = time.perf_counter()
        try:
            async with asyncio.timeout(1):
                await client.get(url)
        except TimeoutError as e:
            print(f"asyncio.timeout(1): {type(e).__name__} за {time.perf_counter() - start:.1f} с")
    srv.close()

    srv = await asyncio.start_server(silent, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{srv.sockets[0].getsockname()[1]}/"
    async with httpx.AsyncClient(timeout=httpx.Timeout(0.3)) as client:
        try:
            await client.get(url)
        except httpx.TimeoutException as e:
            print(f"str: {str(e)!r}")
            print(f"repr: {e!r}")
    srv.close()


asyncio.run(main())
```

```text title="Вывод"
Timeout(1): ответ 200 за 3.0 с
asyncio.timeout(1): TimeoutError за 1.0 с
str: ''
repr: ReadTimeout('')
```

- `Timeout(1)` ни разу не сработал: между байтами проходит 0,3 с, меньше секунды. Запрос занял 3 секунды.
- `asyncio.timeout(1)` оборвал попытку ровно на секунде. С ретраями худший случай — `попыток × таймаут попытки`, его легко посчитать и сверить с бюджетом вызывающего.
- `str()` у `httpx.ReadTimeout` пустая: в логе после `logger.error("ошибка: %s", e)` будет «ошибка: ». Логируйте `repr(e)` или `type(e).__name__`.
- `asyncio.timeout` бросает голый `TimeoutError`, а не `httpx.TimeoutException`: поймать нужно оба, если ретраи настроены на `httpx`-исключения.

Для `requests` то же устройство: `timeout=3` — это connect и read по отдельности, а не весь запрос. Исключения устроены так (смотрим исходник `requests/exceptions.py` версии 2.34, здесь не запускалось): `class ConnectTimeout(ConnectionError, Timeout)` — наследует сразу оба. Поэтому порядок `except` решает: `except ConnectionError` перед `except Timeout` поймает и таймаут подключения, и недоступность хоста; чтобы отличить перегрузку от недоступности, `ConnectTimeout` ловят отдельной веткой первой. `ReadTimeout` наследует только `Timeout`.

## Ограничение параллелизма на 100 000 задач

`gather(*(handle(x) for x in items))` разворачивает генератор сразу: все 100 000 корутин и задач создаются до первого `await`, и память растёт пропорционально числу элементов, даже если за раз нужны десять. Вариант с N воркерами создаёт задач ровно N, а элементы берёт из общего итератора:

```python
import asyncio
import tracemalloc

N = 100_000


async def handle(x: int) -> int:
    await asyncio.sleep(0.01)  # здесь — await client.get(...)
    return x


async def with_gather() -> None:
    await asyncio.gather(*(handle(x) for x in range(N)))  # все корутины и задачи сразу


async def with_workers(workers: int = 100) -> None:
    items = iter(range(N))  # один общий итератор

    async def worker() -> None:
        for x in items:  # next() синхронный: между проверкой и выдачей нет await
            await handle(x)

    async with asyncio.TaskGroup() as tg:
        for _ in range(workers):
            tg.create_task(worker())


for fn in (with_gather, with_workers):
    tracemalloc.start()
    asyncio.run(fn())
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"{fn.__name__:13} пик {peak / 1e6:.1f} МБ")
```

```text title="Вывод"
with_gather   пик 160.5 МБ
with_workers  пик 0.2 МБ
```

Пиковая память по `tracemalloc` (число с этой машины; важен порядок: около 1,6 КБ на каждую из 100 000 задач против почти ничего). Гонок у воркеров нет: цикл событий однопоточный, а `next()` у итератора синхронный, так что два воркера не могут получить один и тот же элемент, переключение возможно только на `await`. Ограничение параллелизма задаёт число воркеров; `Semaphore` поверх этого не нужен. Но воркеры должны читать именно общий итератор (`for x in items`), а не каждый свой `range`.

## Блокирующий код в `async def` останавливает цикл

Задача-«тикалка» просыпается каждые 10 мс и замеряет, насколько опоздала. Под ней — три способа потратить по полсекунды (последний — тяжёлый regex с катастрофическим откатом):

```python
import asyncio
import re
import time


async def ticker(gaps: list[float]) -> None:
    last = time.perf_counter()
    while True:
        await asyncio.sleep(0.01)
        now = time.perf_counter()
        gaps.append(now - last - 0.01)
        last = now


async def blocking_call() -> None:
    time.sleep(0.5)  # синхронный SDK внутри async def


async def threaded_call() -> None:
    await asyncio.to_thread(time.sleep, 0.5)


async def heavy_regex() -> None:
    re.match(r"(a+)+$", "a" * 25 + "!")  # катастрофический откат


async def measure(coro_fn) -> None:
    gaps: list[float] = []
    t = asyncio.create_task(ticker(gaps))
    await asyncio.sleep(0.1)
    await coro_fn()
    await asyncio.sleep(0.1)
    t.cancel()
    print(f"{coro_fn.__name__:14} худшая задержка тика {max(gaps) * 1000:.0f} мс")


async def main() -> None:
    for fn in (blocking_call, threaded_call, heavy_regex):
        await measure(fn)


asyncio.run(main())
```

```text title="Вывод"
blocking_call  худшая задержка тика 492 мс
threaded_call  худшая задержка тика 1 мс
heavy_regex    худшая задержка тика 768 мс
```

Синхронный `time.sleep` (так же — вызов синхронного SDK, например клиента HubSpot) в `async def` остановил весь цикл на полсекунды, остальные задачи и запросы стояли; та же пауза через `to_thread` задержку почти не меняет. Вычисление, которое не отдаёт управление (regex, парсинг, сериализация большого объекта), ведёт себя как блокирующий вызов, и `to_thread` помогает только когда оно отпускает GIL; для чистых вычислений нужен процесс. Для хендлеров FastAPI есть дешёвый вариант: обычный `def` вместо `async def` исполняется в пуле потоков. Время задержек — с этой машины.

## Что спросят дальше

- **Зачем таймаут на фазу, а не один на запрос?** Соединение, отправка, ожидание ответа и чтение имеют разную природу: общий таймаут не отличает медленный DNS от зависшего сервера.
- **Как не запустить 100 000 задач одновременно?** `Semaphore` вокруг тела задачи, а для огромных списков — очередь с фиксированным числом воркеров, чтобы не создавать все задачи заранее.
