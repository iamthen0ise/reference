---
title: "Python: список, генератор или async-генератор"
---

Как выбрать, во что складывать последовательность: в список, в генератор, в итератор-класс, в async-генератор или в `asyncio.Queue`, и что ломается, когда выбрали не то. Четыре рецепта: конвейер чтения большого файла, пакеты через `itertools.batched`, чтение API по страницам async-генератором и генератор, который держит ресурс. Потом восемь типичных поломок, у каждой короткая модель с симптомом: пустой второй проход, ошибка не там, где испорчены данные, незакрытый генератор, потерянный `return`, `len()`, изменение коллекции на обходе и `zip`, который молча обрезает. Протокол итерации и теорию здесь не пересказываем.
Рядом: [[Итераторы и генераторы]] · [[Скачать много ссылок]] · [[asyncio на практике]] · [[Декоратор замера]] · [[Починка декораторов]] · [[Потерянные исключения в async]]
Разделы: [[#Таблица выбора]] · [[#Конвейер чтения большого файла]] · [[#Пакеты через batched]] · [[#Страницы API как async-генератор]] · [[#Генератор с ресурсом]] · [[#Второй проход даёт пусто]] · [[#Ошибка всплывает не там]] · [[#Незакрытый генератор]] · [[#Незакрытый async-генератор]] · [[#Потерянный return]] · [[#Нет len и индексации]] · [[#Изменение коллекции на обходе]] · [[#zip молча обрезает]] · [[#Что спросят дальше]]

| Симптом | Как найти | Что сделать |
| --- | --- | --- |
| Второй раз данных нет | пустой `list` | [[#Второй проход даёт пусто\|фабрика]] |
| Ошибка далеко от причины | traceback в потребителе | [[#Ошибка всплывает не там\|проверка на входе]] |
| Файл не закрыт | `ResourceWarning` в dev | [[#Незакрытый генератор\|closing]] |
| Предупреждение при выходе цикла | `aclose` не awaited | [[#Незакрытый async-генератор\|aclosing]] |
| `return` пропал | `for` его не видит | [[#Потерянный return\|yield from]] |
| `len` и `[0]` падают | `TypeError` | [[#Нет len и индексации\|список, islice]] |
| Элементы пропущены | правка на обходе | [[#Изменение коллекции на обходе\|новая коллекция]] |
| Пары пропали молча | разная длина | [[#zip молча обрезает\|strict]] |
| Резать поток на порции | пакеты по N | [[#Пакеты через batched\|batched]] |
| Данные по страницам | курсор в ответе | [[#Страницы API как async-генератор\|async-генератор]] |

Все блоки запускаются как есть. Вывод получен на Python 3.15, все блоки прогнаны и на 3.12; где вывод отличается, это написано под блоком. Пути во временной папке и в стандартной библиотеке в выводе заменены на `<tmp>` и `<stdlib>`.

## Таблица выбора

Выбор идёт по четырём вопросам: сколько данных, нужен ли второй проход, нужна ли ленивость и откуда данные приходят.

| Что взять | Размер | Повтор | Ленивость | Источник |
| --- | --- | --- | --- | --- |
| список | в памяти | да | нет | память |
| генератор | любой | нет | да | файл |
| итератор-класс | любой | можно | да | состояние |
| async-генератор | любой | нет | да | сеть |
| `asyncio.Queue` | `maxsize` | нет | буфер | задачи |

Колонка «Повтор» отвечает на вопрос, можно ли пройти данные второй раз. У итератора-класса можно, если `__iter__` возвращает новый итератор.

Короткое правило: хватает памяти и нужен второй проход, берите список. Данных много или поток однократный, берите генератор. Источник по сети и каждый элемент требует `await`, берите async-генератор. Несколько независимых производителей и потребителей, нужна очередь с границей, берите `asyncio.Queue`. Итератор-класс пишут редко: когда состояние неудобно держать в локальных переменных функции. Протокол итерации и жизненный цикл генератора разобраны в [[Итераторы и генераторы]].

## Конвейер чтения большого файла

Файл на миллионы строк в список читать незачем. Цепочка генераторов держит в памяти одну строку, а каждое звено делает одно дело: читает, фильтрует, разбирает. Файл открывается внутри первого генератора через `with`, поэтому он закрывается, когда обход закончился. Блок сам создаёт файл во временной папке и удаляет её.

```python
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

ERROR_LEVEL = "ERROR"


def read_lines(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            yield line.rstrip("\n")


def only_errors(lines: Iterable[str]) -> Iterator[str]:
    return (line for line in lines if f" {ERROR_LEVEL} " in line)


def parse(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    for line in lines:
        stamp, _level, message = line.split(" ", 2)
        yield stamp, message


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "app.log"
        log.write_text(
            "".join(
                f"10:{i:02d} {'ERROR' if i % 3 == 0 else 'INFO'} событие {i}\n"
                for i in range(1, 100_001)
            ),
            encoding="utf-8",
        )
        total = 0
        first: list[tuple[str, str]] = []
        for stamp, message in parse(only_errors(read_lines(log))):
            total += 1
            if len(first) < 2:
                first.append((stamp, message))
        print(f"ошибок: {total}")
        print(f"первые: {first}")
    print(f"файл удалён: {not log.exists()}")


main()
```

```text title="Вывод"
ошибок: 33333
первые: [('10:03', 'событие 3'), ('10:06', 'событие 6')]
файл удалён: True
```

Вывод на 3.12 совпадает. Цикл `for` в `main` тянет данные: пока он не просит следующий элемент, ни одно звено ничего не делает. Если из цикла выйти раньше, смотрите [[#Генератор с ресурсом]].

## Пакеты через batched

Базу и API удобно кормить порциями. `itertools.batched(iterable, n)` отдаёт кортежи по `n` элементов, последний может быть короче. Функция появилась в 3.12 («What's New in Python 3.12»).

```python
from itertools import batched

ids = range(1, 8)
for batch in batched(ids, 3):
    print(batch)
print(type(next(iter(batched(ids, 3)))))
```

```text title="Вывод"
(1, 2, 3)
(4, 5, 6)
(7,)
<class 'tuple'>
```

Вывод на 3.12 совпадает. Параметр `strict=True` добавлен в 3.13 («What's New in Python 3.13»): он бросает `ValueError`, если последний пакет неполный.

```python
from itertools import batched

try:
    for batch in batched(range(1, 8), 3, strict=True):
        print(batch)
except (ValueError, TypeError) as exc:
    print(type(exc).__name__, exc)
```

```text title="Вывод"
(1, 2, 3)
(4, 5, 6)
ValueError batched(): incomplete batch
```

Вывод получен на 3.15, на 3.13 он тот же. На 3.12 параметра нет: блок печатает `TypeError batched() takes at most 2 arguments (3 given)`. Если проверка нужна на 3.12, сравните длину последнего пакета сами.

## Страницы API как async-генератор

Постраничный API прячут за async-генератором: потребитель пишет один `async for` и не знает про курсоры. Между страницами есть `await`, поэтому обычный генератор тут не подойдёт. Запрос имитирует `asyncio.sleep`. Генератор закрывается через `aclosing`, чтобы ранний `break` не оставил его приостановленным.

```python
import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing

PAGE_SIZE = 3
TOTAL = 8
LATENCY = 0.05
FAKE_API = list(range(TOTAL))


async def fetch_page(cursor: int) -> tuple[list[int], int | None]:
    await asyncio.sleep(LATENCY)  # имитация сетевого запроса
    items = FAKE_API[cursor : cursor + PAGE_SIZE]
    next_cursor = cursor + PAGE_SIZE
    return items, (next_cursor if next_cursor < TOTAL else None)


async def iter_items() -> AsyncIterator[int]:
    cursor: int | None = 0
    try:
        while cursor is not None:
            items, cursor = await fetch_page(cursor)
            for item in items:
                yield item
    finally:
        print("страницы закончились или нас закрыли")


async def main() -> None:
    async with aclosing(iter_items()) as items:
        async for item in items:
            print("получен", item)
            if item == 4:
                break  # потребитель ушёл раньше конца
    print("вышли из цикла")


asyncio.run(main())
```

```text title="Вывод"
получен 0
получен 1
получен 2
получен 3
получен 4
страницы закончились или нас закрыли
вышли из цикла
```

Вывод на 3.12 совпадает. `break` на элементе 4 происходит на второй странице; `aclosing` вызвал `aclose()`, и сработал `finally`. Тот же приём поверх пула воркеров, с ранним выходом потребителя, показан в разделе «Ранний выход потребителя» заметки [[Скачать много ссылок]].

## Генератор с ресурсом

Если генератор держит файл или соединение, закрытием занимается сам генератор: `with` и `try/finally` внутри. Снаружи потребитель гарантирует, что закрытие произойдёт даже при раннем выходе: `contextlib.closing` для обычного генератора и `contextlib.aclosing` для async. Оба вызывают `close()` или `aclose()` на выходе из блока, те бросают в генератор `GeneratorExit`, и `finally` выполняется сразу, а не когда доберётся сборщик.

```python
import asyncio
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing, closing
from pathlib import Path


def numbers(path: Path) -> Iterator[int]:
    with path.open(encoding="utf-8") as f:
        try:
            for line in f:
                yield int(line)
        finally:
            print("  генератор: закрываю файл", flush=True)


async def ticks() -> AsyncIterator[int]:
    try:
        for i in range(10):
            await asyncio.sleep(0)
            yield i
    finally:
        print("  async-генератор: освобождаю ресурс", flush=True)


async def main(path: Path) -> None:
    print("sync: closing")
    with closing(numbers(path)) as gen:
        for n in gen:
            if n == 2:
                break
    print("sync: после with")
    print("async: aclosing")
    async with aclosing(ticks()) as agen:
        async for t in agen:
            if t == 2:
                break
    print("async: после with")


with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "nums.txt"
    p.write_text("1\n2\n3\n4\n", encoding="utf-8")
    asyncio.run(main(p))
```

```text title="Вывод"
sync: closing
  генератор: закрываю файл
sync: после with
async: aclosing
  async-генератор: освобождаю ресурс
async: после with
```

Вывод на 3.12 совпадает. Сообщение из `finally` напечатано до «после with»: закрытие случилось в момент выхода из блока.

## Второй проход даёт пусто

Генератор и любой итератор исчерпываются. Второй `list(squares)` не падает и не предупреждает, он возвращает пустой список, и код дальше «успешно» работает на нуле данных. Найти это можно по пустому результату там, где данные точно были; лечится фабрикой (функция, которая каждый раз создаёт новый генератор) или списком, если памяти хватает.

```python
squares = (n * n for n in range(4))
print("первый проход:", list(squares))
print("второй проход:", list(squares))

# повторный проход нужен — берём список или фабрику
def make_squares():
    return (n * n for n in range(4))

print("через фабрику:", list(make_squares()), list(make_squares()))
```

```text title="Вывод"
первый проход: [0, 1, 4, 9]
второй проход: []
через фабрику: [0, 1, 4, 9] [0, 1, 4, 9]
```

Вывод на 3.12 совпадает.

## Ошибка всплывает не там

Генератор ленив, поэтому испорченные данные ломают потребителя, а не код, который их передал. Вызов `parse_ages(rows)` ничего не делает: тело функции начнёт выполняться на первом `next`. В traceback первым стоит потребитель, и ошибка кажется его виной; но второй кадр указывает на строку внутри генератора. Если ошибка данных должна быть видна на границе, проверьте данные там сразу, обычным списком, и отдавайте дальше проверенное.

```python
import traceback
from collections.abc import Iterable, Iterator


def parse_ages(rows: Iterable[str]) -> Iterator[int]:
    for row in rows:
        yield int(row)  # ошибка в данных возникнет здесь...


rows = ["31", "27", "двадцать", "40"]
ages = parse_ages(rows)  # ...но не при этом вызове
print("генератор создан, ошибки нет")

try:
    print("сумма:", sum(ages))  # ...а когда кто-то начал читать
except ValueError as exc:
    print("ValueError:", exc)
    for frame in traceback.extract_tb(exc.__traceback__):
        print(f"  кадр {frame.name}, строка {frame.lineno}")

# Проверка на границе: загрузить и проверить сразу, потом отдавать дальше
def parse_ages_eager(rows: Iterable[str]) -> list[int]:
    return [int(row) for row in rows]

try:
    parse_ages_eager(rows)
except ValueError as exc:
    print("на границе:", exc)
```

```text title="Вывод"
генератор создан, ошибки нет
ValueError: invalid literal for int() with base 10: 'двадцать'
  кадр <module>, строка 15
  кадр parse_ages, строка 7
на границе: invalid literal for int() with base 10: 'двадцать'
```

Вывод на 3.12 совпадает. Ранняя проверка годится, когда данные на входе небольшие. Для потока, который не влезает в память, добавляйте контекст к ошибке (номер строки) внутри генератора.

## Незакрытый генератор

Плохой вариант: ресурс открыт без `with`, а закрытие стоит после цикла. Если потребитель бросил генератор на полпути, `GeneratorExit` приходит на `yield`, до `f.close()` исполнение не доходит, и файл остаётся открытым: пока на генератор есть ссылка, он так и висит; когда последняя ссылка ушла, файл закрывает лишь деструктор, и с предупреждением. Симптомов два. Первый: `inspect.getgeneratorstate` показывает `GEN_SUSPENDED`, то есть генератор начат и не закончен, а `getgeneratorlocals` показывает, что `f` ещё жив. Второй: под `python -X dev` приходит `ResourceWarning: unclosed file`. Без `-X dev` предупреждение по умолчанию скрыто, поэтому в обычном запуске утечку не видно.

```python
import inspect
import tempfile
from collections.abc import Iterator
from pathlib import Path


def lines_no_finally(path: Path) -> Iterator[str]:
    f = path.open(encoding="utf-8")  # открыли, закрытие — после цикла
    for line in f:
        yield line
    f.close()  # до этой строки при раннем выходе не дойдём


with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "data.txt"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    gen = lines_no_finally(p)
    print("состояние до старта:", inspect.getgeneratorstate(gen))
    print("первая строка:", next(gen).strip())
    print("состояние:", inspect.getgeneratorstate(gen))
    print("локальные:", sorted(inspect.getgeneratorlocals(gen)))
    keep = [gen]  # ссылка живёт в списке (так бывает: поле объекта, кэш, цикл ссылок)
    del gen
    print("локальное имя удалено, состояние:", inspect.getgeneratorstate(keep[0]))
    keep.clear()  # последняя ссылка ушла: файл закрыл лишь деструктор, с предупреждением
    print("список очищен")
print("конец программы")
```

```text title="Вывод под python -u -X dev, stdout и stderr вперемешку"
состояние до старта: GEN_CREATED
первая строка: a
состояние: GEN_SUSPENDED
локальные: ['f', 'line', 'path']
локальное имя удалено, состояние: GEN_SUSPENDED
c3_leak.py:25: ResourceWarning: unclosed file <_io.TextIOWrapper name='<tmp>/data.txt' mode='r' encoding='utf-8'>
  keep.clear()  # последняя ссылка ушла: файл закрыл лишь деструктор, с предупреждением
ResourceWarning: Enable tracemalloc to get the object allocation traceback
список очищен
конец программы
```

Без `-X dev` те же строки, кроме трёх строк предупреждения. На 3.12 с `-X dev` вывод совпадает. Лечение: `with` внутри генератора, у потребителя `closing` или явный `close()`. С `with` файл закрывается сразу, как только вызван `close()`:

```python
import inspect
import tempfile
from collections.abc import Iterator
from pathlib import Path


def lines_with(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8") as f:
        yield from f


with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "data.txt"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    gen = lines_with(p)
    next(gen)
    f = inspect.getgeneratorlocals(gen)["f"]
    print("файл открыт, closed =", f.closed)
    gen.close()
    print("после gen.close(): state =", inspect.getgeneratorstate(gen), "closed =", f.closed)
```

```text title="Вывод"
файл открыт, closed = False
после gen.close(): state = GEN_CLOSED closed = True
```

Вывод на 3.12 и под `-X dev` совпадает. В CPython потеря последней ссылки на генератор вызывает `close()`, а кадр уничтожается вместе с файлом, поэтому простой `del gen` файл «закроет» (с предупреждением). Настоящая утечка там, где ссылка живёт долго: поле объекта, кэш, список, цикл ссылок. Полагаться на сборщик нельзя: на других реализациях момент закрытия не определён.

## Незакрытый async-генератор

Для async-генератора закрытие само требует `await`: `aclose()` это корутина. Если потребитель вышел по `break` без `aclosing`, генератор остаётся приостановленным, и закрыть его сможет только цикл событий. Внутри `asyncio.run` цикл перед выходом вызывает `shutdown_asyncgens()` и всё закрывает. Предупреждение появляется, когда цикл закрыли руками без этого вызова. Модель так и делает: свой цикл, `break` без `aclosing`, `loop.close()`.

```python
import asyncio
import gc
from collections.abc import AsyncIterator


async def ticks() -> AsyncIterator[int]:
    try:
        for i in range(10):
            yield i
    finally:
        await asyncio.sleep(0)  # освобождение требует await
        print("ticks: finally выполнен", flush=True)


async def consume() -> None:
    async for t in ticks():
        if t == 1:
            break  # без aclosing: генератор остаётся приостановленным
    print("вышли из цикла", flush=True)


loop = asyncio.new_event_loop()
loop.run_until_complete(consume())
gc.collect()
print("gc.collect() сделан", flush=True)
loop.close()  # закрыли цикл, shutdown_asyncgens() не вызывали
print("цикл закрыт", flush=True)
```

```text title="Вывод под python -u, stdout и stderr вперемешку"
вышли из цикла
gc.collect() сделан
Task was destroyed but it is pending!
task: <Task pending name='Task-2' coro=<<async_generator_athrow without __name__>()>>
<stdlib>/asyncio/base_events.py:749: RuntimeWarning: coroutine method 'aclose' of 'ticks' was never awaited
  self._ready.clear()
RuntimeWarning: Enable tracemalloc to get the object allocation traceback
цикл закрыт
```

Строки «finally выполнен» в выводе нет: освобождение ресурса не случилось. Сборщик запланировал `aclose()` на цикле, цикл закрыли раньше, чем задача выполнилась, и asyncio пожаловался на уничтоженную задачу. На 3.12 вывод короче: нет двух предупреждений `RuntimeWarning`, остаются `Task was destroyed but it is pending!` и строка `task: ...`. Под `-X dev` к сообщению добавляется `source_traceback`, где создана задача. Лечение: `aclosing` вокруг генератора и `asyncio.run` вместо ручного цикла; если цикл ручной, перед `loop.close()` нужно выполнить корутину `loop.shutdown_asyncgens()` через `run_until_complete`.

## Потерянный return

Генератор может вернуть значение, но `for` его не увидит: оно лежит в `StopIteration.value`. В модели цикл печатает только элементы, сумма пропала. Забрать её можно вручную, поймав `StopIteration`, или через `yield from` в другом генераторе: это выражение равно возвращаемому значению.

```python
from collections.abc import Generator


def summing(items: list[int]) -> Generator[int, None, int]:
    total = 0
    for item in items:
        total += item
        yield item
    return total  # уходит в StopIteration.value


print("for:", [x for x in summing([1, 2, 3])])  # return value пропал

gen = summing([1, 2, 3])
try:
    while True:
        next(gen)
except StopIteration as stop:
    print("вручную:", stop.value)


def wrapper(items: list[int]) -> Generator[int, None, None]:
    total = yield from summing(items)  # yield from возвращает значение
    print("через yield from:", total)


print(list(wrapper([1, 2, 3])))
```

```text title="Вывод"
for: [1, 2, 3]
вручную: 6
через yield from: 6
[1, 2, 3]
```

Вывод на 3.12 совпадает. Если значение нужно потребителю часто, отдавайте его иначе: последним элементом, объектом-накопителем или отдельной функцией. Про `yield from` подробнее в разделе «yield from, send, throw, close» заметки [[Итераторы и генераторы]].

## Нет len и индексации

Генератор не знает своей длины и не хранит элементы, поэтому `len(gen)` и `gen[0]` дают `TypeError`. Первые элементы берите через `itertools.islice`; размер получите, собрав список, но только когда данные влезают в память. `islice` потребляет элементы, остаток после него короче.

```python
from itertools import islice

gen = (n for n in range(10))
for expr in ("len(gen)", "gen[0]"):
    try:
        eval(expr)
    except TypeError as exc:
        print(f"{expr}: {exc}")

print("первые три:", list(islice(gen, 3)))
print("остаток:", len(list(gen)))
```

```text title="Вывод"
len(gen): object of type 'generator' has no len()
gen[0]: 'generator' object is not subscriptable
первые три: [0, 1, 2]
остаток: 7
```

Вывод на 3.12 совпадает.

## Изменение коллекции на обходе

Список при удалении сдвигается, и цикл молча пропускает следующий элемент: удалили `bad1`, а `bad2` остался. Словарь строже и бросает `RuntimeError`. Симптом у списка тихий, поэтому его легко пропустить. Чинится сборкой новой коллекции (включение) или обходом копии.

```python
users = ["ann", "bob", "bad1", "bad2", "cat"]
for u in users:
    if u.startswith("bad"):
        users.remove(u)  # список сдвигается, следующий элемент пропущен
print("список:", users)

scores = {"a": 1, "b": 0, "c": 0}
try:
    for k, v in scores.items():
        if v == 0:
            del scores[k]
except RuntimeError as exc:
    print("словарь:", exc)

scores = {"a": 1, "b": 0, "c": 0}
scores = {k: v for k, v in scores.items() if v != 0}  # собрали новый
print("новый словарь:", scores)
```

```text title="Вывод"
список: ['ann', 'bob', 'bad2', 'cat']
словарь: dictionary changed size during iteration
новый словарь: {'a': 1}
```

Вывод на 3.12 совпадает.

## zip молча обрезает

`zip` останавливается по самой короткой последовательности и ничего не сообщает: третье имя без возраста просто исчезло. С `strict=True` длины сверяются, и расхождение даёт `ValueError`. Параметр есть с 3.10 («What's New in Python 3.10»). Для данных, которые по смыслу одной длины, ставьте его всегда.

```python
names = ["ann", "bob", "cat"]
ages = [31, 27]
print("без strict:", list(zip(names, ages)))
try:
    print(list(zip(names, ages, strict=True)))
except ValueError as exc:
    print("strict=True:", exc)
```

```text title="Вывод"
без strict: [('ann', 31), ('bob', 27)]
strict=True: zip() argument 2 is shorter than argument 1
```

Вывод на 3.12 совпадает. Если длины заведомо разные и обрезка нужна, `itertools.zip_longest` или явный `islice` скажут об этом читателю кода.

## Что спросят дальше

- Чем генератор отличается от списка? Он ленивый и одноразовый, считает по требованию и хранит одно значение. Протокол и устройство описаны в [[Итераторы и генераторы]].
- Как закрыть генератор, из которого вышли раньше? `close()` бросает в него `GeneratorExit`; снаружи это делает `contextlib.closing`, для async-генератора `aclosing`.
- Почему утечка файла не видна? `ResourceWarning` по умолчанию скрыт, включается под `python -X dev`.
- Что делает `return` в генераторе? Кладёт значение в `StopIteration.value`; `for` его не показывает, `yield from` отдаёт вызывающему.
- Когда `asyncio.Queue`, а когда async-генератор? Очередь связывает независимые задачи и ограничивает буфер, генератор прячет последовательное чтение источника за `async for`.
- Как резать поток на порции? `itertools.batched` с 3.12, с 3.13 есть `strict=True`.
