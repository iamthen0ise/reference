---
title: "Python: asyncio на практике"
---

Как писать асинхронный код без сюрпризов: `TaskGroup`, `gather`, забытый `await`, контекст запроса. Ошибки и отмена, таймауты и нагрузка вынесены на соседние страницы.
Рядом: [[asyncio под капотом]] · [[Ошибки и отмена в asyncio]] · [[Таймауты и нагрузка в asyncio]] · [[Потоки и процессы]]
Разделы: gather и TaskGroup: что с остальными задачами, когда одна упала · Забытый await · ContextVar в двух задачах · [[#Что спросят дальше]]

```python
import asyncio
import time

async def fetch(i: int, limit: asyncio.Semaphore) -> int:
    async with limit:  # не больше N запросов одновременно
        await asyncio.sleep(0.05)  # здесь — await client.get(url)
        return i

def blocking_report() -> str:
    time.sleep(0.05)  # синхронная библиотека
    return "report"

async def main() -> None:
    limit = asyncio.Semaphore(10)
    async with asyncio.timeout(5):  # общий дедлайн на всю пачку
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(fetch(i, limit)) for i in range(100)]
            report = tg.create_task(asyncio.to_thread(blocking_report))
    print(sum(t.result() for t in tasks), report.result())

asyncio.run(main())
```

- **`asyncio.run(main())`** создаёт цикл событий, выполняет корутину, отменяет оставшиеся задачи, закрывает асинхронные генераторы и сам цикл. Одна точка входа на программу.
- **`TaskGroup` (3.11+) вместо `gather`.** Если одна задача группы упала, остальные отменяются, а ошибки приходят вместе как `ExceptionGroup` (ловят через `except*`). `gather` по умолчанию отдаёт первое исключение, а остальные задачи продолжают работать сами по себе, без присмотра; с `return_exceptions=True` возвращает исключения в списке результатов.
- **`Semaphore`** ограничивает число одновременных запросов: без него `create_task` на 10 000 URL откроет 10 000 соединений, и внешний API ответит 429 или упадёт.
- **`asyncio.timeout(...)`** (3.11+) или `wait_for` — дедлайн. По истечении задача внутри отменяется, наружу вылетает `TimeoutError`.
- **Блокирующий код — в поток**: `await asyncio.to_thread(fn, *args)` или `loop.run_in_executor(pool, fn)`. Вычисления — в `ProcessPoolExecutor` через тот же `run_in_executor`.

## `gather` и `TaskGroup`: что с остальными задачами, когда одна упала

Три задачи: одна падает через 0,1 с, две другие работают дольше.

```python
import asyncio

async def job(name: str, delay: float, fail: bool = False) -> str:
    try:
        await asyncio.sleep(delay)
        if fail:
            raise ValueError(f"{name} упала")
        print(name, "готова")
        return name
    except asyncio.CancelledError:
        print(name, "отменена")
        raise

def specs():
    return [job("fail", 0.1, fail=True), job("slow", 0.5), job("fast", 0.3)]

async def with_gather() -> None:
    try:
        await asyncio.gather(*specs())
    except ValueError as e:
        print("gather поймал:", e)
    await asyncio.sleep(0.6)  # ждём: кто-то ещё доработает?

async def with_group() -> None:
    try:
        async with asyncio.TaskGroup() as tg:
            for c in specs():
                tg.create_task(c)
    except* ValueError as eg:
        print("TaskGroup поймал:", eg.exceptions[0])

print("--- gather")
asyncio.run(with_gather())
print("--- TaskGroup")
asyncio.run(with_group())
```

```text title="Вывод"
--- gather
gather поймал: fail упала
fast готова
slow готова
--- TaskGroup
fast отменена
slow отменена
TaskGroup поймал: fail упала
```

После ошибки `gather` просто отдал исключение, а остальные задачи доработали сами, уже никем не ожидаемые: их результат потерян, а побочные эффекты случились. `TaskGroup` отменил соседей и вышел только после того, как они завершились.

## Забытый `await`

```python
import asyncio

async def fetch(i: int) -> int:
    return i

async def main() -> None:
    fetch(1)  # забыли await
    print("дальше")

asyncio.run(main())
```

```text title="Вывод"
a1.py:9: RuntimeWarning: coroutine 'fetch' was never awaited
  fetch(1)  # забыли await
RuntimeWarning: Enable tracemalloc to get the object allocation traceback
дальше
```

Предупреждение пришло из `stderr` сразу при удалении корутины, а программа пошла дальше как ни в чём не бывало.

## `ContextVar` в двух задачах

```python
import asyncio
import contextvars

request_id = contextvars.ContextVar("request_id", default="-")

async def handle(rid: str) -> None:
    request_id.set(rid)
    await asyncio.sleep(0.01)  # пока ждём, вторая задача успела записать своё
    print(rid, "видит", request_id.get())

async def main() -> None:
    await asyncio.gather(handle("A"), handle("B"))
    print("снаружи:", request_id.get())

asyncio.run(main())
```

```text title="Вывод"
A видит A
B видит B
снаружи: -
```

`gather` оборачивает каждую корутину в задачу с копией контекста, поэтому `set` в одной не виден другой и не виден снаружи. С глобальной переменной обе задачи увидели бы значение той, что записала последней.

## Что спросят дальше

- **Как вызвать async из sync и наоборот?** Из синхронного кода — `asyncio.run(coro())` (если цикл ещё не запущен) или `asyncio.run_coroutine_threadsafe(coro, loop).result()` из другого потока. Синхронное из асинхронного — `await asyncio.to_thread(fn)`.
- **`gather`, `wait`, `as_completed`, `TaskGroup` — в чём разница?** `gather` — результаты в порядке задач; `as_completed` — по мере готовности; `wait` — низкоуровневый, возвращает множества готовых и неготовых задач, умеет `FIRST_COMPLETED`; `TaskGroup` — структурная конкурентность: группа не завершится, пока не завершились все её задачи, и падение одной отменяет остальные.
