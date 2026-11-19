---
title: "Python: декораторы, генераторы, контекстные менеджеры"
---

Конспект к собеседованию по трём механизмам Python, которые встречаются в любом бэкенд-коде и на которые чаще всего дают задачу «напиши прямо сейчас»: декораторы, итераторы и генераторы, контекстные менеджеры. У каждого есть асинхронный вариант, и его спрашивают сразу следом.
Рядом: [[Python-классы]] (создание объектов, MRO, `__slots__`) · [[Python-конкурентность]] (потоки, процессы, asyncio, GIL).
Разделы: [[#Декораторы]] · [[#Итераторы и генераторы]] · [[#Асинхронные итераторы и генераторы]] · [[#Контекстные менеджеры]] · [[#contextlib]] · [[#Что спросят дальше]]

**Суть.** Все три механизма — это протоколы: Python не требует наследоваться от какого-то класса, а просто вызывает методы с заранее известными именами. `for` вызывает `__iter__` и `__next__`, `with` вызывает `__enter__` и `__exit__`, а `@` перед функцией просто вызывает объект, который стоит после него. Поэтому любой свой объект с нужными методами работает с `for`, `with` и `@` так же, как встроенный. Асинхронные версии устроены так же, только методы называются `__aiter__`, `__anext__`, `__aenter__`, `__aexit__`, и внутри них можно ждать (`await`).

## Декораторы

Декоратор — это функция, которая принимает функцию и возвращает другую функцию, обычно обёртку вокруг исходной. Запись

```python
@timed
def load_report(day): ...
```

ровно то же самое, что `load_report = timed(load_report)`. Отсюда два следствия, о которых спрашивают:

- **Декоратор выполняется один раз, при определении функции**, то есть обычно при импорте модуля. При каждом вызове выполняется уже обёртка, которую он вернул. Поэтому код в теле декоратора (вне обёртки) — место для регистрации функции (так работают `@app.get(...)` во FastAPI и `@pytest.fixture`), но не для логики вызова.
- **Несколько декораторов применяются снизу вверх, а выполняются сверху вниз.** `@a @b def f` — это `f = a(b(f))`: при вызове сначала срабатывает внешняя обёртка `a`, она вызывает обёртку `b`, та — исходную `f`.

Обёртка видит исходную функцию через **замыкание** (closure): вложенная функция запоминает переменные внешней функции, в которой её создали, даже после того как внешняя функция вернула управление.

### Наивный декоратор и functools.wraps

```python
def naive(func):
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper


@naive
def charge(user_id: int, amount: int) -> None:
    """Списать деньги."""


print(charge.__name__, charge.__doc__)  # wrapper None
```

Наивная обёртка подменяет функцию целиком: снаружи видны имя `wrapper`, пустая документация и сигнатура `(*args, **kwargs)`. Что от этого ломается:

- логи и трейсы в Sentry показывают `wrapper` вместо настоящего имени;
- FastAPI строит параметры эндпоинта по сигнатуре функции: увидев `*args, **kwargs`, он не поймёт, какие параметры брать из запроса;
- Celery и pickle находят функцию по имени (`__qualname__`) и с чужим именем не находят;
- `help()` и IDE не показывают документацию и подсказки.

`functools.wraps(func)` — это тоже декоратор, он копирует на обёртку `__name__`, `__qualname__`, `__doc__`, `__module__`, `__annotations__`, `__type_params__`, дополняет `__dict__` и кладёт ссылку на оригинал в `__wrapped__`. Функция `inspect.signature` идёт по `__wrapped__` до исходной функции, поэтому FastAPI и IDE видят настоящие параметры, а в тестах до исходной функции можно добраться через `charge.__wrapped__`. Правило простое: `@functools.wraps(func)` есть в каждом декораторе, который возвращает обёртку.

### Пример: логгер времени выполнения

```python
import functools
import logging
import time

log = logging.getLogger(__name__)


def timed(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - start
            log.info("%s took %.3f s", func.__qualname__, elapsed)

    return wrapper


@timed
def load_report(day: str) -> list[str]:
    """Загрузить отчёт за день."""
    time.sleep(0.1)
    return [day]
```

Детали, за которые хвалят:

- `time.perf_counter()`, а не `time.time()`: `time.time()` — это настенные часы, их может перевести NTP (синхронизация времени по сети), и разница между двумя замерами получится отрицательной. `perf_counter` монотонный и самый точный.
- `try/finally`: время пишется в лог, даже если функция упала. Иначе медленные падения не видны вовсе.
- `log.info("%s took %.3f s", ...)` с аргументами, а не f-строкой: строка соберётся, только если уровень INFO включён.
- Результат функции возвращается (`return func(...)`). Частая ошибка на собеседовании — вызвать `func(...)` и забыть вернуть результат, тогда задекорированная функция всегда возвращает `None`.

### Декоратор с параметрами: повтор при ошибках с экспоненциальной задержкой

Если декоратору нужны настройки (`@retry(attempts=5)`), появляется третий уровень вложенности: `retry(...)` — это фабрика, она принимает настройки и возвращает настоящий декоратор, а тот уже принимает функцию.

```python
import functools
import logging
import random
import time

log = logging.getLogger(__name__)


def retry(
    exceptions: tuple[type[Exception], ...],
    attempts: int = 3,
    base_delay: float = 0.1,
    factor: float = 2.0,
    max_delay: float = 5.0,
):
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 1
            while True:
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt >= attempts:
                        raise
                    cap = min(max_delay, base_delay * factor ** (attempt - 1))
                    delay = random.uniform(0, cap)
                    log.warning(
                        "%s: attempt %d/%d failed: %r, retry in %.2f s",
                        func.__qualname__, attempt, attempts, exc, delay,
                    )
                    time.sleep(delay)
                    attempt += 1

        return wrapper

    return decorator


@retry((ConnectionError, TimeoutError), attempts=5, base_delay=0.2)
def fetch_rate(currency: str) -> float:
    ...
```

Как это работает и почему так:

- **Ловим только переданные исключения.** `except exceptions` с кортежем классов ловит эти классы и их наследников, всё остальное пролетает сразу, без повторов. Повторять имеет смысл только временные сбои: обрыв соединения, таймаут, ответ 503. Ошибку в данных (`ValueError`, ответ 400) повтор не исправит, он только задержит ответ в несколько раз.
- **На последней попытке — голый `raise`.** Он пробрасывает то же исключение с исходным стеком вызовов (traceback). `raise exc` тоже сработает, но добавит в стек лишнюю строку; оборачивать в новое исключение без `from exc` нельзя — потеряется причина.
- **Экспоненциальная задержка (exponential backoff).** Пауза растёт как `base_delay * factor ** (попытка - 1)`: 0,2 → 0,4 → 0,8 с, и ограничена сверху `max_delay`, чтобы на десятой попытке не ждать полчаса.
- **Джиттер (jitter, случайный разброс).** `random.uniform(0, cap)` — так называемый full jitter: пауза берётся случайно от нуля до потолка. Без разброса сто клиентов, упавших в одну секунду, будут повторять тоже синхронно и снова положат сервис. Подробнее про эту лавину — в [[Kafka#Откуда берётся снежный ком|конспекте по Kafka]].
- **Тип — `Exception`, а не `BaseException`.** `BaseException` включает `KeyboardInterrupt`, `SystemExit` и `asyncio.CancelledError`: декоратор, который их ловит и повторяет, не даст остановить программу или отменить задачу.
- **Повторять можно только идемпотентную операцию** (такую, которую безопасно выполнить дважды). Таймаут не значит, что запрос не дошёл: возможно, деньги уже списаны, а потерялся только ответ. Для неидемпотентных запросов нужен ключ идемпотентности, см. [[Redis#Структуры данных и задачи|Redis]].

Частая ошибка — написать `@retry` без скобок. Тогда функция попадает в параметр `exceptions`, `retry` возвращает `decorator`, и при вызове `fetch_rate("USD")` строка `"USD"` уйдёт в `decorator` вместо функции. Если декоратор должен работать в обеих формах, первым параметром принимают функцию с умолчанием `None`:

```python
import functools


def timed(func=None, *, threshold: float = 0.0):
    if func is None:  # вызвали со скобками: @timed(threshold=1.0)
        return functools.partial(timed, threshold=threshold)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper
```

### Синхронный и асинхронный декоратор

Асинхронная функция (`async def`) при вызове не выполняется, а возвращает объект-корутину; код внутри пойдёт, только когда корутину ждут через `await`. Поэтому обычный декоратор на асинхронной функции тихо ломается:

- `timed` замерит только создание корутины — около нуля секунд — и вернёт её наружу, а настоящая работа пройдёт уже вне замера;
- `retry` никогда не поймает ошибку: исключение возникает при `await`, то есть снаружи `try`;
- `time.sleep` внутри обёртки остановит весь цикл событий (event loop), и вместе с этой задачей встанут все остальные. В асинхронном коде ждать нужно через `await asyncio.sleep(...)`.

Асинхронной функции нужна асинхронная обёртка: `async def wrapper` и `await func(...)` внутри. Универсальный декоратор при применении смотрит, какую функцию ему дали, и возвращает подходящую обёртку:

```python
import asyncio
import functools
import inspect
import logging
import random
import time

log = logging.getLogger(__name__)


def retry(
    exceptions: tuple[type[Exception], ...],
    attempts: int = 3,
    base_delay: float = 0.1,
    factor: float = 2.0,
    max_delay: float = 5.0,
):
    def delay_for(attempt: int) -> float:
        return random.uniform(0, min(max_delay, base_delay * factor ** (attempt - 1)))

    def decorator(func):
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                attempt = 1
                while True:
                    try:
                        return await func(*args, **kwargs)
                    except exceptions:
                        if attempt >= attempts:
                            raise
                        await asyncio.sleep(delay_for(attempt))
                        attempt += 1

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            attempt = 1
            while True:
                try:
                    return func(*args, **kwargs)
                except exceptions:
                    if attempt >= attempts:
                        raise
                    time.sleep(delay_for(attempt))
                    attempt += 1

        return sync_wrapper

    return decorator


@retry((ConnectionError,), attempts=3)
async def fetch_rate(currency: str) -> float:
    await asyncio.sleep(0.01)
    return 1.0
```

- `inspect.iscoroutinefunction`, а не `asyncio.iscoroutinefunction`: вариант из `asyncio` устарел с Python 3.14. `functools.wraps` асинхронность не скрывает, так что декораторы можно ставить друг на друга.
- Отмена задачи приходит в корутину как `asyncio.CancelledError` на том `await`, где она стоит. Это наследник `BaseException`, поэтому `except exceptions` его не поймает, и отмена пройдёт сквозь повторы. Так и должно быть.
- Асинхронный `timed` пишется так же: `start = time.perf_counter()`, `await func(...)` в `try`, замер в `finally`. Он покажет реальное время до ответа, включая время, пока цикл событий выполнял другие задачи.

### Типизация декоратора

Без аннотаций mypy и IDE видят результат декоратора как функцию с неизвестными параметрами. `ParamSpec` сохраняет параметры исходной функции (синтаксис `[**P, R]` — с Python 3.12):

```python
import functools
import time
from collections.abc import Callable


def timed[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            print(func.__qualname__, time.perf_counter() - start)

    return wrapper
```

Для асинхронного варианта сигнатура `Callable[P, Awaitable[R]] -> Callable[P, Awaitable[R]]`, а если декоратор меняет параметры (например, подставляет сессию БД), меняющиеся параметры описывают через `Concatenate`.

### Класс как декоратор и декоратор класса

- **Класс как декоратор.** `@CountCalls` вызывает `CountCalls(func)`, а вызов задекорированной функции попадает в `__call__` экземпляра. Удобно, когда у декоратора есть состояние, которое нужно видеть снаружи (`charge.calls`). Подвох: на методе класса такой декоратор теряет `self`, потому что экземпляр класса — не функция и не умеет привязываться к объекту. Чинится методом `__get__` (это протокол дескрипторов, пример — в [[Python-классы#Как Python ищет атрибут]]), но для методов проще писать функцию-декоратор.
- **Декоратор класса** принимает класс, а не функцию: `@dataclass` дописывает классу `__init__`, `__repr__`, `__eq__`; `@functools.total_ordering` достраивает сравнения.

Где декораторы встречаются в стандартной библиотеке и вокруг: `functools.cache` и `lru_cache` (запоминание результатов), `property`, `classmethod`, `staticmethod`, `dataclass`, `contextlib.contextmanager`, `pytest.fixture`, `app.get` во FastAPI, `celery.task`.

## Итераторы и генераторы

### Протокол итерации

Два понятия, которые путают:

- **Итерируемый объект (iterable)** — то, по чему можно пройти циклом: у него есть `__iter__`, который возвращает итератор. Список, строка, словарь, `range`, файл.
- **Итератор (iterator)** — объект, который выдаёт элементы по одному: `__next__` возвращает следующий элемент или бросает `StopIteration`, когда элементы кончились. Ещё у итератора есть `__iter__`, который возвращает его самого, поэтому итератор тоже итерируемый.

Цикл `for x in obj` разворачивается так:

```python
obj = [1, 2, 3]

it = iter(obj)            # obj.__iter__()
while True:
    try:
        x = next(it)      # it.__next__()
    except StopIteration:
        break
    print(x)
```

Главное отличие: итерируемый объект можно обходить сколько угодно раз, каждый `for` получает новый итератор. Итератор одноразовый: он помнит позицию и после исчерпания больше ничего не отдаёт.

```python
squares = (x * x for x in range(5))
print(sum(squares))  # 30
print(sum(squares))  # 0 — итератор уже исчерпан, ошибки нет
```

Эта ловушка встречается в реальном коде: генератор передали в две функции, вторая молча получила пустоту. Ещё `x in iterator` поглощает элементы до найденного, а `len()` у итератора нет.

### Итератор вручную и генератор

Итератор, написанный классом:

```python
class Countdown:
    """Итерируемый: каждый for получает свой итератор."""

    def __init__(self, start: int) -> None:
        self.start = start

    def __iter__(self):
        return CountdownIterator(self.start)


class CountdownIterator:
    def __init__(self, current: int) -> None:
        self.current = current

    def __iter__(self):
        return self

    def __next__(self) -> int:
        if self.current <= 0:
            raise StopIteration
        self.current -= 1
        return self.current + 1


print(list(Countdown(3)), list(Countdown(3)))  # [3, 2, 1] [3, 2, 1]
```

То же самое генератором:

```python
def countdown(start: int):
    while start > 0:
        yield start
        start -= 1


print(list(countdown(3)))  # [3, 2, 1]
```

**Генератор** — функция с `yield` внутри. Вызов такой функции не выполняет её тело, а возвращает объект-генератор, который является итератором. При каждом `next()` тело выполняется до следующего `yield`, отдаёт значение и замирает: локальные переменные и позиция сохраняются в кадре стека (frame) генератора. Когда функция доходит до конца или до `return`, генератор бросает `StopIteration`.

Генератор — самый короткий способ написать итератор: Python сам создаёт `__iter__` и `__next__` и сам хранит состояние. Чтобы сделать многоразовый итерируемый класс, достаточно написать `__iter__` генератором:

```python
class Countdown:
    def __init__(self, start: int) -> None:
        self.start = start

    def __iter__(self):
        n = self.start
        while n > 0:
            yield n
            n -= 1
```

| | Итератор-класс | Генератор |
| --- | --- | --- |
| Как написать | класс с `__iter__` и `__next__` | функция с `yield` или выражение `(x for x in ...)` |
| Где состояние | в атрибутах объекта, вручную | в кадре функции, автоматически |
| Дополнительные методы | любые свои (`reset`, `peek`) | `send`, `throw`, `close` |
| Когда брать | нужно состояние, видимое снаружи, или свои методы | почти всегда |

### Зачем нужны генераторы: ленивость и конвейеры

Генератор вычисляет элементы по запросу (лениво) и держит в памяти один элемент, а не весь список. Поэтому файл на 10 ГБ можно обработать за постоянный объём памяти, а бесконечную последовательность — описать вообще.

```python
def read_lines(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            yield line.rstrip("\n")


def parse(lines):
    for line in lines:
        user_id, amount = line.split(",")
        yield user_id, int(amount)


def batched(items, size):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def import_payments(path):
    for batch in batched(parse(read_lines(path)), 500):
        print(len(batch))  # здесь — вставка пачки в БД
```

Каждый шаг конвейера берёт итератор и отдаёт итератор, ни один не держит файл в памяти целиком. Строки читаются из файла, только когда следующий шаг попросил очередной элемент. Готовый `batched` есть в стандартной библиотеке — `itertools.batched` (Python 3.12), но на собеседовании его часто просят написать.

Когда генератор не подходит: если данные нужны несколько раз, нужна длина, доступ по индексу или сортировка — берите список. Генераторное выражение `(x for x in data)` вычисляет `data` сразу, а всё остальное лениво.

### yield from, send, throw, close

- **`yield from sub`** отдаёт наружу все элементы `sub`, как цикл `for x in sub: yield x`. Но вдобавок пробрасывает во вложенный генератор `send` и `throw` и возвращает значение его `return`:

```python
def inner():
    yield 1
    yield 2
    return "done"


def outer():
    result = yield from inner()
    print("inner вернул", result)


print(list(outer()))  # inner вернул done, затем [1, 2]
```

- **`gen.send(value)`** продолжает генератор и передаёт значение внутрь: выражение `yield` возвращает то, что прислали. Первый вызов — `next(gen)` или `send(None)`, чтобы дойти до первого `yield`.

```python
def running_avg():
    total = count = 0
    avg = None
    while True:
        value = yield avg
        total += value
        count += 1
        avg = total / count


g = running_avg()
next(g)              # дойти до первого yield
print(g.send(10))    # 10.0
print(g.send(20))    # 15.0
```

- **`gen.throw(exc)`** бросает исключение внутри генератора, в точке, где стоит `yield`. **`gen.close()`** бросает туда `GeneratorExit`, чтобы выполнились блоки `finally` и `with`. Сборщик мусора вызывает `close()` сам, когда генератор больше никому не нужен.
- `return value` в генераторе превращается в `StopIteration(value)`. А если внутри генератора случайно вылетает `StopIteration` (например, из `next()` без значения по умолчанию), Python превращает его в `RuntimeError` — иначе генератор молча закончился бы раньше времени (PEP 479).

`send` и `yield from` — исторический фундамент async: до Python 3.5 корутины писали генераторами (`@asyncio.coroutine` и `yield from`), а `await` устроен почти как `yield from`. Подробно, с игрушечным циклом событий, — в [[Python-конкурентность#Async под капотом]].

## Асинхронные итераторы и генераторы

Если очередной элемент нужно ждать (следующая страница API, сообщение из очереди, строка из сетевого сокета), обычный `__next__` не подходит: он синхронный и заблокирует цикл событий. Для этого есть асинхронный протокол:

- `__aiter__` — **обычный** (не `async`) метод, возвращает асинхронный итератор;
- `async def __anext__` — возвращает следующий элемент, а в конце бросает `StopAsyncIteration`;
- обходят такой объект через `async for`, и только внутри `async def`.

```python
import asyncio


class Ticker:
    def __init__(self, count: int, delay: float) -> None:
        self.count = count
        self.delay = delay
        self.i = 0

    def __aiter__(self):
        return self

    async def __anext__(self) -> int:
        if self.i >= self.count:
            raise StopAsyncIteration
        await asyncio.sleep(self.delay)
        self.i += 1
        return self.i


async def main():
    async for tick in Ticker(3, 0.01):
        print(tick)


asyncio.run(main())
```

**Асинхронный генератор** — `async def` с `yield` внутри. Это самый частый вариант в жизни, например постраничное чтение API:

```python
import asyncio
from contextlib import aclosing

PAGES = {None: ([1, 2], "p2"), "p2": ([3, 4], "p3"), "p3": ([5], None)}


async def get_page(cursor):
    await asyncio.sleep(0.01)  # здесь — HTTP-запрос
    return PAGES[cursor]


async def fetch_all():
    cursor = None
    while True:
        items, cursor = await get_page(cursor)
        for item in items:
            yield item
        if cursor is None:
            return


async def main():
    async with aclosing(fetch_all()) as items:
        async for item in items:
            if item == 4:
                break
            print(item)
    print([x async for x in fetch_all()])  # асинхронное списковое выражение


asyncio.run(main())
```

Чем отличается от синхронного генератора:

- нет `yield from` и нельзя `return value` (только голый `return`);
- методы называются `asend`, `athrow`, `aclose`, и их нужно ждать через `await`;
- **очистка при раннем выходе.** Если потребитель вышел из `async for` по `break` или исключению, `finally` внутри генератора выполнится не сразу, а когда цикл событий соберёт генератор. С асинхронным генератором это особенно неприятно: очистка (закрыть соединение, вернуть его в пул) должна сделать `await`, а значит, может случиться позже, в другой задаче или не случиться вовсе при остановке программы. Поэтому генератор, который держит ресурс, оборачивают в `contextlib.aclosing(...)`: он вызовет `aclose()` сразу при выходе из блока.

## Контекстные менеджеры

Контекстный менеджер гарантирует, что ресурс будет освобождён, как бы ни закончился блок кода: нормально, через `return`, `break` или исключение. Файлы, блокировки, транзакции, соединения из пула, временные настройки — всё это открывают через `with`.

Протокол — два метода:

- `__enter__(self)` вызывается на входе в блок; то, что он вернул, попадает в переменную после `as`;
- `__exit__(self, exc_type, exc, tb)` вызывается на выходе. Если блок завершился без ошибки, все три аргумента — `None`. Если с ошибкой — класс исключения, само исключение и стек вызовов. Если `__exit__` вернёт истинное значение, исключение **подавляется**, и программа идёт дальше после `with`.

Вот во что примерно разворачивается `with mgr as value: body(value)`:

```python
mgr = Timer()
value = type(mgr).__enter__(mgr)
try:
    body(value)
except BaseException as exc:
    if not type(mgr).__exit__(mgr, type(exc), exc, exc.__traceback__):
        raise
else:
    type(mgr).__exit__(mgr, None, None, None)
```

Пример: таймер и транзакция.

```python
import sqlite3
import time


class Timer:
    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.elapsed = time.perf_counter() - self.start
        return False  # исключение не подавляем


class Transaction:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False


conn = sqlite3.connect(":memory:", isolation_level=None)
conn.execute("CREATE TABLE t (x INTEGER)")
with Timer() as t, Transaction(conn) as tx:
    tx.execute("INSERT INTO t VALUES (1)")
print(f"{t.elapsed:.6f} s")
```

Тонкости, которые проверяют:

- **Если упал `__enter__`, `__exit__` не вызывается.** Блок `with` ещё не начался. Поэтому `__enter__` должен либо захватить всё, либо ничего: если он открыл два соединения и упал на третьем, первые два нужно закрыть внутри самого `__enter__` (или собрать их через `ExitStack`, см. ниже).
- **Возвращать `True` из `__exit__` опасно**: так менеджер глотает любые ошибки блока, включая опечатки в коде. Подавлять стоит только конкретное ожидаемое исключение, как делает `contextlib.suppress`.
- **Исключение внутри `__exit__` заменяет исходное.** Исходное не теряется полностью, оно сохраняется в `__context__` нового исключения и видно в стеке как «During handling of the above exception...», но ловить будут уже новое.
- **Несколько менеджеров в одном `with`** открываются слева направо, закрываются в обратном порядке. С Python 3.10 их можно перечислить в скобках на нескольких строках.
- **Многоразовость.** Объект файла и менеджер из `@contextmanager` одноразовые; `threading.Lock` многоразовый, но не реентерабельный (повторный вход из того же потока — взаимная блокировка, deadlock); `threading.RLock` реентерабельный.

### Асинхронный контекстный менеджер

Если вход или выход требуют ожидания (взять соединение из пула, отправить `COMMIT` по сети, закрыть HTTP-сессию), нужен асинхронный протокол: `async def __aenter__` и `async def __aexit__`, а вход в блок — `async with`. Стандартные примеры: `asyncio.Lock`, `asyncio.timeout(...)`, `asyncio.TaskGroup`, `aiohttp.ClientSession`, транзакции в asyncpg и SQLAlchemy.

```python
class AsyncTransaction:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def __aenter__(self):
        await self.conn.execute("BEGIN")
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        await self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        return False
```

Обычный `with` с асинхронным менеджером не работает, и наоборот. Если задачу отменили внутри блока, в `__aexit__` придёт `asyncio.CancelledError`; ей нужно дать пройти дальше, то есть не возвращать `True`.

## contextlib

Модуль `contextlib` позволяет не писать класс с `__enter__` и `__exit__` ради каждого менеджера.

### @contextmanager и @asynccontextmanager

Менеджер пишется генератором с ровно одним `yield`: код до `yield` — это вход, значение `yield` попадает в `as`, код после — выход.

```python
import logging
import time
from contextlib import contextmanager

log = logging.getLogger(__name__)


@contextmanager
def timer(label: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        log.info("%s: %.3f s", label, time.perf_counter() - start)


with timer("report"):
    time.sleep(0.01)


@timer("job")  # этот же менеджер работает как декоратор
def job():
    time.sleep(0.01)


job()
```

Как это устроено. `contextmanager` оборачивает генератор в объект с `__enter__` и `__exit__`. `__enter__` вызывает `next()` и доходит до `yield`. Если блок `with` завершился нормально, `__exit__` снова вызывает `next()`, генератор доходит до конца. Если блок упал, `__exit__` бросает исключение внутрь генератора через `gen.throw()` — оно вылетает из строки с `yield`. Отсюда главная ловушка:

- **без `try/finally` вокруг `yield` код очистки при исключении не выполнится**: исключение вылетит из `yield` и пролетит мимо всего, что ниже;
- чтобы подавить исключение, его ловят вокруг `yield` и не пробрасывают дальше;
- два `yield` — ошибка `RuntimeError: generator didn't stop`.

Объект, который создаёт `@contextmanager`, наследует `ContextDecorator`, поэтому менеджер можно повесить на функцию как декоратор (пример с `job` выше): при каждом вызове создаётся новый менеджер. На асинхронную функцию так вешать нельзя — синхронный менеджер обернёт только создание корутины, как в разделе про декораторы.

Асинхронный вариант — `@asynccontextmanager`: генератор с `async def`, внутри можно `await`.

```python
import asyncio
from contextlib import asynccontextmanager


class FakeConn:
    async def execute(self, sql: str) -> None:
        print(sql)


class FakePool:
    async def acquire(self) -> FakeConn:
        return FakeConn()

    async def release(self, conn: FakeConn) -> None:
        print("released")


@asynccontextmanager
async def transaction(pool: FakePool):
    conn = await pool.acquire()
    try:
        await conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            await conn.execute("ROLLBACK")
            raise
        else:
            await conn.execute("COMMIT")
    finally:
        await pool.release(conn)


async def main():
    async with transaction(FakePool()) as conn:
        await conn.execute("INSERT ...")


asyncio.run(main())
```

Здесь `except BaseException` уместен: при отмене задачи (`CancelledError`) транзакцию тоже нужно откатить, а голый `raise` пробрасывает исключение дальше и ничего не глотает.

### ExitStack: сколько ресурсов — неизвестно заранее

`ExitStack` — менеджер, в который можно складывать другие менеджеры во время работы. На выходе он закрывает их все в обратном порядке, даже если один из них упал при закрытии.

```python
from contextlib import ExitStack


def merge(paths: list[str], out_path: str) -> None:
    with ExitStack() as stack:
        files = [stack.enter_context(open(p, encoding="utf-8")) for p in paths]
        out = stack.enter_context(open(out_path, "w", encoding="utf-8"))
        for f in files:
            out.writelines(f)
```

Если третий файл не открылся, первые два закроются. Ещё у стека есть `stack.callback(fn, *args)` — зарегистрировать любую функцию очистки, и `stack.pop_all()` — забрать накопленные ресурсы в новый стек и не закрывать их сейчас. Последнее нужно для `__enter__`, который захватывает несколько ресурсов: при ошибке на середине стек закроет захваченное, а при успехе ресурсы передаются объекту и закроются в его `__exit__`. Асинхронный вариант — `AsyncExitStack` с `enter_async_context`.

### Остальное

| Инструмент | Что делает | Пример |
| --- | --- | --- |
| `suppress(*exc)` | подавляет перечисленные исключения | `with suppress(FileNotFoundError): os.remove(path)` |
| `closing(obj)` / `aclosing(obj)` | вызывает `obj.close()` / `await obj.aclose()` на выходе | объекты без своего `with`, асинхронные генераторы |
| `nullcontext(value)` | менеджер, который ничего не делает | `with lock if lock else nullcontext():` — необязательная блокировка |
| `redirect_stdout(f)` | временно подменяет `sys.stdout` | перехватить вывод чужой функции в тесте |
| `chdir(path)` | временно меняет текущую директорию (3.11+) | скрипты; меняет состояние всего процесса, в многопоточном коде опасно |
| `ContextDecorator` / `AsyncContextDecorator` | базовый класс: свой менеджер-класс работает и как декоратор | `class timed(ContextDecorator)` |

## Что спросят дальше

- **Чем `@lru_cache` на методе плох?** Кэш хранит аргументы вызова, включая `self`, поэтому объекты никогда не освобождаются — утечка памяти (линтер ruff ловит это правилом B019). Для свойства, которое считается один раз на объект, есть `functools.cached_property`; для общего кэша — функция вне класса или кэш с ограниченным размером и временем жизни.
- **Где хранить состояние декоратора, например счётчик вызовов?** В замыкании с `nonlocal count` или атрибутом обёртки (`wrapper.calls = 0`). Состояние общее для всех вызовов функции, а в многопоточном коде его изменение нужно защищать блокировкой.
- **`range` — итератор или генератор?** Ни то, ни другое. Это ленивая последовательность: у неё есть `len`, доступ по индексу, проверка `in` за O(1), и её можно обходить много раз.
- **Что будет, если генератор держит ресурс (`with open` внутри), а его не дочитали?** Файл останется открытым, пока генератор жив. Закрыть явно — `gen.close()` или `contextlib.closing(gen)`; для асинхронного генератора — `aclosing`.
- **Генератор или список в аргументе функции?** Если функция проходит по данным один раз — принимайте любой iterable, это гибче. Если несколько раз — либо явно требуйте последовательность (`Sequence`), либо сделайте `list(items)` в начале.
- **Контекстный менеджер или `try/finally`?** Одно и то же по смыслу. Менеджер нужен, когда пара «захватить — освободить» повторяется в нескольких местах или когда освобождение легко забыть.
- **Что вернёт функция, если `return` есть и в `try`, и в `finally`?** Значение из `finally`: он выполняется последним и перекрывает `return` из `try`, а заодно глотает летящее исключение. С Python 3.14 `return` в `finally` даёт предупреждение (PEP 765).
- **Как протестировать функцию под декоратором `retry`, чтобы тест не спал?** Вызвать исходную функцию через `func.__wrapped__` или подменить `time.sleep` (`asyncio.sleep`) в тесте; ещё лучше — передать задержку параметром и в тесте поставить ноль.
