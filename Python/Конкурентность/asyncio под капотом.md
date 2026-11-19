---
title: "Python: asyncio под капотом"
---

Что происходит у `async`/`await` внутри: корутина, цикл событий, задача, Future и как цикл событий устроен своими руками.
Рядом: [[Итераторы и генераторы]] (генераторы, на которых выросли корутины) · [[asyncio на практике]] · [[Потоки и процессы]]
Разделы: [[#Зачем вообще async]] · [[#Четыре понятия]] · [[#Цикл событий своими руками]] · [[#Что спросят дальше]]

## Зачем вообще async

Поток на каждое соединение — простая модель, но на десяти тысячах соединений дорогая: у каждого потока свой стек (резервируются мегабайты адресного пространства), а переключает их ОС с полным сохранением контекста. При этом почти всё время потоки просто ждут сеть.

Идея async: один поток, неблокирующие сокеты и системный вызов, который ждёт сразу на всех сокетах и возвращает те, что готовы (`epoll` в Linux, `kqueue` в macOS; в Python — модуль `selectors`). Когда задаче нужно ждать, она отдаёт управление, и поток занимается другими задачами. Переключение — это просто вызов функции в том же потоке, без участия ОС.

## Четыре понятия

- **Корутина (coroutine).** Функция, объявленная через `async def`, — корутинная функция; её вызов ничего не выполняет, а возвращает объект-корутину. Корутина умеет приостанавливаться на `await` и продолжать с того же места. Устроена как генератор (см. [[Итераторы и генераторы#yield from, send, throw, close]]): её «прокручивают» методом `send(None)`, а на точке ожидания она отдаёт наружу объект, которого ждёт.
- **Future.** Объект-обещание: результата ещё нет, но будет. Хранит состояние (ожидает, готов, отменён), результат или исключение и список колбэков, которые вызовутся при завершении. `await future` приостанавливает корутину, пока кто-то не вызовет `future.set_result(...)`. В прикладном коде напрямую почти не встречается: на нём строятся библиотеки.
- **Task.** Наследник Future, который ведёт корутину: вызывает её `send`, пока она не завершится. Если корутина остановилась на Future, задача подписывается на него и продолжит корутину, когда Future станет готов. Результат задачи — то, что вернула корутина. Создаётся через `asyncio.create_task(coro)` или `TaskGroup.create_task(coro)`, и с этого момента корутина выполняется **конкурентно**, не дожидаясь `await`.
- **Цикл событий (event loop).** Планировщик в одном потоке. В бесконечном цикле: 1) выполнить все готовые колбэки из очереди; 2) посчитать, сколько можно ждать до ближайшего таймера; 3) спросить ОС, какие сокеты готовы, с этим таймаутом; 4) поставить колбэки готовых сокетов и истёкших таймеров в очередь. «Шаг задачи» — это просто колбэк в этой очереди.

`await x` работает с любым awaitable-объектом: корутиной, Task, Future или объектом с методом `__await__`. `await` на корутине — как `yield from`: выполнение идёт внутрь, вплоть до самого нижнего Future, который отдаётся наверх, через все корутины, до Task.

## Цикл событий своими руками

Игрушечный asyncio на 50 строк: Future, Task, цикл с таймерами и `sleep`. В настоящем цикле вместо `time.sleep` стоит ожидание сокетов через `selectors` с тем же таймаутом.

```python
import heapq
import time
from collections import deque


class Future:
    def __init__(self) -> None:
        self.done = False
        self.result = None
        self.callbacks = []

    def set_result(self, value) -> None:
        self.done = True
        self.result = value
        for callback in self.callbacks:
            loop.call_soon(callback)

    def __await__(self):
        if not self.done:
            yield self  # отдать себя наверх, до Task: «разбуди, когда я буду готов»
        return self.result


class Task:
    def __init__(self, coro) -> None:
        self.coro = coro
        loop.call_soon(self.step)

    def step(self) -> None:
        try:
            future = self.coro.send(None)  # выполнить корутину до следующего await
        except StopIteration:
            return  # корутина закончилась
        future.callbacks.append(self.step)  # продолжить, когда Future будет готов


class Loop:
    def __init__(self) -> None:
        self.ready = deque()
        self.timers = []  # куча (when, seq, callback)
        self.seq = 0

    def call_soon(self, callback) -> None:
        self.ready.append(callback)

    def call_later(self, delay: float, callback) -> None:
        self.seq += 1
        heapq.heappush(self.timers, (time.monotonic() + delay, self.seq, callback))

    def run(self) -> None:
        while self.ready or self.timers:
            if not self.ready:  # делать нечего — ждём ближайший таймер
                time.sleep(max(0.0, self.timers[0][0] - time.monotonic()))
            now = time.monotonic()
            while self.timers and self.timers[0][0] <= now:
                self.ready.append(heapq.heappop(self.timers)[2])
            for _ in range(len(self.ready)):
                self.ready.popleft()()


loop = Loop()


def sleep(delay: float) -> Future:
    future = Future()
    loop.call_later(delay, lambda: future.set_result(None))
    return future


async def worker(name: str, delay: float) -> None:
    print(name, "start")
    await sleep(delay)
    print(name, "done after", delay)


Task(worker("a", 0.2))
Task(worker("b", 0.1))
started = time.monotonic()
loop.run()
print(f"total {time.monotonic() - started:.1f} s")  # 0.2, а не 0.3: ожидания шли одновременно
```

Что видно из этого кода и что стоит проговорить:

- **`await` сам по себе ничего не распараллеливает.** `await a(); await b()` выполняет `a` и `b` строго по очереди. Конкурентность появляется только от нескольких задач (`create_task`, `gather`, `TaskGroup`).
- **Переключение возможно только на `await`.** Между двумя `await` код задачи выполняется без перерыва, поэтому в asyncio гонок по данным меньше, чем в потоках. Но схема «проверить, `await`, действовать» ломается так же, как с потоками.
- **Блокирующий вызов останавливает всех.** `time.sleep(1)`, `requests.get`, синхронный драйвер БД или тяжёлый цикл внутри корутины не отдают управление циклу, и все остальные задачи стоят. Режим отладки (`asyncio.run(main(), debug=True)` или `PYTHONASYNCIODEBUG=1`) пишет в лог колбэки дольше 100 мс.

## Что спросят дальше

- **Почему asyncio не ускоряет вычисления?** Один поток, и пока корутина считает, она не доходит до `await` и не отдаёт управление. Вычисления выносят в процессы через `run_in_executor` или в отдельный сервис/очередь задач.
- **Потоки или asyncio для 10 000 соединений?** asyncio: задача весит килобайты, переключение — вызов функции. Потоков столько обычно не держат: память и переключения ОС. Но asyncio требует, чтобы вся цепочка была асинхронной: один синхронный драйвер БД — и выигрыш пропал.
- **Что такое uvloop?** Замена стандартного цикла событий, написанная на Cython поверх libuv (того же, что в Node.js); быстрее в 2–4 раза на сетевом вводе-выводе. Uvicorn ставит его, если он установлен.
