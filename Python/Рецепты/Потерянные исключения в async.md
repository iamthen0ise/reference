---
title: "Python: потерянные исключения в async"
---

Куда в asyncio пропадают исключения и логи и как их вернуть: задача умерла молча, «Task exception was never retrieved» приходит поздно, фоновую задачу собрал GC, отмену проглотили, `gather` вернул ошибки в списке, а группа исключений в логе уместилась в одну строку. Для каждой потери есть короткая модель, которая сама завершается и показывает, где именно ошибка появилась или пропала. В конце готовый рецепт `spawn`/`shutdown` для фоновых задач, `trace_id` в каждой строке лога и разбор результатов `gather`. Отмена и группы исключений как теория разобраны в соседних заметках; здесь только то, что видно на практике.
Рядом: [[Ошибки и отмена в asyncio]] · [[asyncio на практике]] · [[Логирование]] · [[Выбор примитива синхронизации]] · [[Починка декораторов]] · [[Декоратор замера]]
Разделы: [[#Исключение задачи никто не забрал]] · [[#Задача без ссылки]] · [[#Корутина без await]] · [[#Проглоченная отмена]] · [[#Режимы отладки]] · [[#Обработчик ошибок цикла]] · [[#Хуки процесса и потока]] · [[#Фоновая задача без потерь]] · [[#trace_id в каждой строке лога]] · [[#Разбор результатов gather]] · [[#ExceptionGroup и add_note]] · [[#Лог, который не дошёл]] · [[#Что спросят дальше]]

| Симптом | Как найти | Что сделать |
| --- | --- | --- |
| Задача умерла, в логе тишина | `task.done()`, но никто не читал | [[#Исключение задачи никто не забрал\|забрать исключение]] |
| Ошибка пришла поздно или никогда | сообщение при сборке задачи | [[#Фоновая задача без потерь\|done-callback]] |
| Задача исчезла посреди работы | «destroyed but it is pending» | [[#Задача без ссылки\|держать ссылку]] |
| Код после вызова не выполнился | «was never awaited» | [[#Корутина без await\|поставить await]] |
| `cancel()` не помог | `done()` ложь после отмены | [[#Проглоченная отмена\|перевыбросить]] |
| Не ясно, кто создал задачу | в сообщении нет места создания | [[#Режимы отладки\|-X dev]] |
| Нужен один обработчик на всё | потери идут мимо логов | [[#Обработчик ошибок цикла\|set_exception_handler]] |
| Ошибка из потока или `__del__` | нет в логе | [[#Хуки процесса и потока\|хуки]] |
| При выходе работа не дописана | задача отменена в `asyncio.run` | [[#Фоновая задача без потерь\|shutdown]] |
| Не понять, чья это строка лога | строки разных запросов вперемешку | [[#trace_id в каждой строке лога\|trace_id]] |
| Ошибки `gather` затерялись | список с исключениями | [[#Разбор результатов gather\|разбор]] |
| Группа ошибок одной строкой | `2 sub-exceptions` | [[#ExceptionGroup и add_note\|exc_info]] |
| Лог из контейнера не пришёл | процесс убит, буфер потерян | [[#Лог, который не дошёл\|буфер]] |

Все блоки запускаются как есть. В выводе пути скрипта убраны, пути стандартной библиотеки заменены на `<stdlib>`, а кадры стека самой библиотеки, где они не нужны, сокращены до `...`. Вывод получен на Python 3.15, те же блоки проверены на 3.12, различия отмечены в тексте.

## Исключение задачи никто не забрал

Корутина, запущенная через `create_task`, живёт сама по себе. Если она упала, исключение сидит внутри объекта задачи и ждёт, пока кто-нибудь вызовет `await task` или `task.exception()`. Никто не вызвал: в этот момент ничего не печатается и не логируется. Сообщение «Task exception was never retrieved» приходит позже и по другой причине: когда задачу собирает сборщик мусора, у неё срабатывает деструктор и он замечает, что исключение так и не забрали. Пока на задачу есть ссылка, ошибка молчит.

```python
import asyncio
import gc


async def boom() -> None:
    raise RuntimeError("упала в фоне")


async def main() -> None:
    task = asyncio.create_task(boom(), name="boom")
    await asyncio.sleep(0.1)  # задача давно завершилась с исключением
    print(f"task.done()={task.done()}, никто не вызывал task.exception()", flush=True)
    del task  # последняя ссылка исчезла
    print("после del", flush=True)
    gc.collect()
    print("после gc.collect()", flush=True)
    await asyncio.sleep(0.1)


asyncio.run(main())
print("конец программы", flush=True)
```

```text title="Вывод"
task.done()=True, никто не вызывал task.exception()
Task exception was never retrieved
future: <Task finished name='boom' coro=<boom() done, defined at never_retrieved.py:5> exception=RuntimeError('упала в фоне')>
Traceback (most recent call last):
  File "never_retrieved.py", line 6, in boom
    raise RuntimeError("упала в фоне")
RuntimeError: упала в фоне
после del
после gc.collect()
конец программы
```

Задача упала почти сразу, но строка `task.done()=True` напечатана раньше любого слова об ошибке. Сообщение пришло ровно на `del task`, когда исчезла последняя ссылка, и ещё до строки «после del». В реальном сервисе ссылка живёт долго, поэтому сообщение приходит через минуты, в неожиданном месте лога, а то и при остановке процесса. Печатается оно в stderr через логгер `asyncio`, а не в логгер приложения: если stderr не собирается, потеря полная.

Лечение: не бросать задачу «в воздух». Либо дожидаться её (`await task`, `TaskGroup`, `gather`), либо повесить done-callback, который логирует ошибку сразу ([[#Фоновая задача без потерь]]). Про `gather` и `TaskGroup` и про то, что остальные задачи делают при падении одной: раздел «`gather` и `TaskGroup`: что с остальными задачами, когда одна упала» заметки [[asyncio на практике]].

## Задача без ссылки

Цикл хранит задачи только по слабым ссылкам. Задачу, на которую больше никто не ссылается, сборщик мусора вправе удалить в любой момент, даже недоделанную. Это написано в документации `asyncio.create_task`. Вызов `create_task(...)` без присваивания часто работает по случайности: задачу держит то, чего она ждёт. А когда не держит, она исчезает молча. Модель ниже делает это детерминированно: `gc.collect()` вместо ожидания, пока сборщик придёт сам.

```python
import asyncio
import gc


async def worker() -> None:
    loop = asyncio.get_running_loop()
    forever = loop.create_future()  # на него больше никто не ссылается
    await forever
    print("сюда не дойдём")


async def main() -> None:
    asyncio.create_task(worker(), name="worker")  # ссылку не сохранили
    await asyncio.sleep(0.05)  # worker стартовал и ждёт future
    print("вызываем gc.collect()", flush=True)
    gc.collect()
    print("gc.collect() вернулся", flush=True)
    await asyncio.sleep(0.05)


asyncio.run(main())
```

```text title="Вывод"
вызываем gc.collect()
Task was destroyed but it is pending!
task: <Task pending name='worker' coro=<worker() done, defined at destroyed_pending.py:5> wait_for=<Future pending cb=[Task.task_wakeup()]>>
gc.collect() вернулся
```

Корутина `worker` ждала future, на который кроме неё никто не ссылался. Задача и future образовали замкнутую группу без внешних ссылок, `gc.collect()` её забрал, и `print("сюда не дойдём")` не выполнится никогда. Предупреждение «Task was destroyed but it is pending!» единственный след. Лечение: хранить ссылку на задачу (множество в модуле, как в `spawn` ниже) или держать задачи в `TaskGroup`, который хранит их сам.

## Корутина без await

Вызов `save("a")` без `await` создаёт объект корутины, но тело функции не выполняется. Python замечает это, когда объект уничтожается, и пишет `RuntimeWarning`.

```python
import asyncio


async def save(item: str) -> None:
    print("сохранил", item)


async def main() -> None:
    save("a")  # забыли await
    print("main пошёл дальше", flush=True)
    await asyncio.sleep(0)


asyncio.run(main())
```

```text title="Вывод"
never_awaited.py:9: RuntimeWarning: coroutine 'save' was never awaited
  save("a")  # забыли await
RuntimeWarning: Enable tracemalloc to get the object allocation traceback
main пошёл дальше
```

Предупреждение пришло сразу, ещё до строки «main пошёл дальше»: объект корутины уничтожился тут же. Слова «сохранил a» нет совсем, потому что тело `save` не запускалось. Если корутину держат в переменной или списке, предупреждение придёт позже, при уничтожении. Подробнее о забытом `await` и о том, как его ловит линтер: раздел «Забытый `await`» заметки [[asyncio на практике]].

## Проглоченная отмена

`task.cancel()` не убивает задачу, а бросает в неё `CancelledError` в точке ожидания. Если задача поймала его и пошла дальше, она продолжит работать, а вызывающий код уверен, что отменил. Теория: раздел «Отмена: проглоченная и проброшенная» заметки [[Ошибки и отмена в asyncio]]. Модель: два воркера, у одного `except CancelledError` с «пошёл дальше», у другого `finally`.

```python
import asyncio

STEPS = 5
STEP_S = 0.1


async def bad_worker() -> str:
    for step in range(STEPS):
        try:
            await asyncio.sleep(STEP_S)
        except asyncio.CancelledError:
            print(f"шаг {step}: поймал отмену и пошёл дальше")
    return "дошёл до конца"


async def good_worker() -> str:
    try:
        for _ in range(STEPS):
            await asyncio.sleep(STEP_S)
        return "дошёл до конца"
    finally:
        print("good: убрал за собой")


async def main() -> None:
    for worker in (bad_worker, good_worker):
        task = asyncio.create_task(worker(), name=worker.__name__)
        await asyncio.sleep(0.15)
        task.cancel()
        await asyncio.sleep(0.05)
        print(f"{task.get_name()}: done={task.done()}, cancelled={task.cancelled()}")
        if not task.done():
            await task
            print(f"{task.get_name()}: результат={task.result()!r}")


asyncio.run(main())
```

```text title="Вывод"
шаг 1: поймал отмену и пошёл дальше
bad_worker: done=False, cancelled=False
bad_worker: результат='дошёл до конца'
good: убрал за собой
good_worker: done=True, cancelled=True
```

После `cancel()` и паузы `bad_worker` всё ещё работает (`done=False`) и в конце спокойно возвращает результат. `good_worker` отменился (`cancelled=True`) и прибрался в `finally`. Правило для рецептов на этой странице: `CancelledError` перехватывать можно только ради уборки и обязательно перевыбрасывать. Поэтому в коде ниже её никто не гасит.

## Режимы отладки

У asyncio есть режим отладки. Его включают четырьмя способами: переменная `PYTHONASYNCIODEBUG=1`, режим разработчика `python -X dev`, `asyncio.run(main(), debug=True)` и `loop.set_debug()` (список в документации «Developing with asyncio»). Что он добавляет, по той же странице документации: проверку потока для небезопасных вызовов, лог долгих операций селектора и лог колбэков, которые держат цикл дольше 100 мс (порог меняется через `loop.slow_callback_duration`). `-X dev` включает отладку asyncio и заодно показывает предупреждения вроде `ResourceWarning`, а `PYTHONASYNCIODEBUG=1` включает только отладку asyncio. Посмотрим, что меняется на уже разобранных моделях.

Забытый `await` под `-X dev` (для сравнения: вывод в разделе про корутину; середина стека цикла пропущена как `...`):

```text title="Вывод: never_awaited.py под -X dev"
never_awaited.py:9: RuntimeWarning: coroutine 'save' was never awaited
Coroutine created at (most recent call last)
  File "never_awaited.py", line 14, in <module>
    asyncio.run(main())
  ...
  File "never_awaited.py", line 9, in main
    save("a")  # забыли await
  save("a")  # забыли await
RuntimeWarning: Enable tracemalloc to get the object allocation traceback
main пошёл дальше
```

Добавился стек «Coroutine created at»: видно, из какой функции создана никем не ожидаемая корутина. Без отладки там была подсказка «Enable tracemalloc».

Потерянное исключение под `-X dev` (то же происходит под `PYTHONASYNCIODEBUG=1`):

```text title="Вывод: never_retrieved.py под -X dev"
task.done()=True, никто не вызывал task.exception()
Task exception was never retrieved
future: <Task finished name='boom' coro=<boom() done, defined at never_retrieved.py:5> exception=RuntimeError('упала в фоне') created at <stdlib>/asyncio/tasks.py:404>
source_traceback: Object created at (most recent call last):
  File "never_retrieved.py", line 20, in <module>
    asyncio.run(main())
  ...
  File "never_retrieved.py", line 10, in main
    task = asyncio.create_task(boom(), name="boom")
  ...
Traceback (most recent call last):
  File "never_retrieved.py", line 6, in boom
    raise RuntimeError("упала в фоне")
RuntimeError: упала в фоне
после del
после gc.collect()
конец программы
```

Добавились `created at ...` в строке задачи и блок `source_traceback`: стек места, где вызвали `create_task`. Теперь ясно, кто породил задачу, а не только где она упала. Для «задачи без ссылки» добавляется тот же `source_traceback`. Цена: заметное замедление, поэтому режим включают на стенде и в тестах, а не в проде.

Медленный колбэк. Блокирующий `time.sleep` внутри корутины держит весь цикл:

```python
import asyncio
import time

BLOCK_S = 0.25


async def handler() -> None:
    time.sleep(BLOCK_S)  # блокирующий вызов в корутине


async def main() -> None:
    await handler()


asyncio.run(main())
```

Без флагов блок ничего не печатает. Под `PYTHONASYNCIODEBUG=1` (то же под `-X dev`):

```text title="Вывод: slow_cb.py под PYTHONASYNCIODEBUG=1"
Executing <Task finished name='Task-1' coro=<main() done, defined at slow_cb.py:11> result=None created at <stdlib>/asyncio/runners.py:110> took 0.261 seconds
```

Строка идёт из логгера `asyncio` на уровне WARNING, поэтому видна даже без настройки логирования. Время зависит от машины: около 0.25 секунды. Название задачи и место, где она создана, показывают, кого искать; о лечении блокировок — раздел «Блокирующий вызов в корутине» заметки [[asyncio на практике]].

Отличие версий в этих моделях только косметическое: текст предупреждений и состав строк на 3.12 тот же, меняются лишь номера строк внутри стандартной библиотеки, например `created at <stdlib>/asyncio/tasks.py:404` на 3.15 и `:420` на 3.12.

## Обработчик ошибок цикла

Если цикл сам встретил ошибку, которую некому обработать (упавший колбэк, задачу собрали с необработанным исключением), он вызывает `loop.call_exception_handler(context)`. По умолчанию это печать через логгер `asyncio`. Своя функция через `loop.set_exception_handler` отправляет то же самое в свой логгер, в Sentry, куда угодно. `context` это словарь: `message`, `exception` (может не быть) и, смотря что случилось, `handle` (колбэк) или `future` (для необработанной задачи там лежит она сама).

```python
import asyncio
import gc
import logging
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app.loop")


def handle_loop_error(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    exc = context.get("exception")
    what = context["message"]
    future = context.get("future")  # для необработанной задачи в контексте лежит она сама
    if future is not None:
        what += f" [task={future.get_name()}]"
    log.error("%s", what, exc_info=exc)


def failing_callback() -> None:
    raise ValueError("упал колбэк")


async def boom() -> None:
    raise RuntimeError("упала в фоне")


async def main() -> None:
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(handle_loop_error)
    loop.call_soon(failing_callback)
    task = asyncio.create_task(boom(), name="boom")
    await asyncio.sleep(0.05)
    del task
    gc.collect()
    await asyncio.sleep(0)


asyncio.run(main())
```

```text title="Вывод"
ERROR app.loop: Exception in callback failing_callback() at loop_handler.py:19
Traceback (most recent call last):
  File "<stdlib>/asyncio/events.py", line 94, in _run
    self._context.run(self._callback, *self._args)
    ~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "loop_handler.py", line 20, in failing_callback
    raise ValueError("упал колбэк")
ValueError: упал колбэк
ERROR app.loop: Task exception was never retrieved [task=boom]
Traceback (most recent call last):
  File "loop_handler.py", line 24, in boom
    raise RuntimeError("упала в фоне")
RuntimeError: упала в фоне
```

Обе потери пошли в наш логгер `app.loop` с трейсбеком, потому что мы передали исключение через `exc_info=exc`. Второе сообщение приходит в момент сборки задачи, как в первом разделе, но теперь оно хотя бы в нужном логе. Первый трейсбек показывает типичную картину «traceback из одних внутренностей цикла»: он начинается в `events.py`, в `Handle._run`, и только потом идёт ваша функция. Такой трейсбек читают с конца: последняя строка перед исключением ваша, всё выше принадлежит циклу. На 3.15 под строкой с `self._context.run(...)` добавлены подчёркивания `~~~^^^`, на 3.12 их нет; смысл тот же.

Обработчик не заменяет done-callback: он срабатывает поздно, при сборке мусора, и только для того, что цикл заметил.

## Хуки процесса и потока

Вне asyncio у Python есть три хука, и каждый ловит своё:

- `sys.excepthook`: исключение дошло до верха главного потока и убивает процесс;
- `threading.excepthook`: исключение дошло до верха любого другого потока (без хука поток просто пишет трейсбек и умирает, процесс живёт);
- `sys.unraisablehook`: исключение, которое некуда поднять, например из `__del__`.

Исключения задач asyncio ни в один из них не попадают: задача сама держит исключение, а дальше им занимается цикл ([[#Обработчик ошибок цикла]]).

```python
import sys
import threading
from types import TracebackType
from typing import Any


def main_hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
    print(f"sys.excepthook: {exc_type.__name__}: {exc}")


def thread_hook(args: threading.ExceptHookArgs) -> None:
    name = args.thread.name if args.thread else "?"
    print(f"threading.excepthook: поток {name}: {args.exc_type.__name__}: {args.exc_value}")


def unraisable_hook(args: "sys.UnraisableHookArgs") -> None:
    print(f"sys.unraisablehook: err_msg задан={args.err_msg is not None}: {args.exc_type.__name__}: {args.exc_value}")


class Closer:
    def __del__(self) -> None:
        raise OSError("не смог закрыть в __del__")


sys.excepthook = main_hook
threading.excepthook = thread_hook
sys.unraisablehook = unraisable_hook


def work() -> None:
    raise RuntimeError("упал поток")


t = threading.Thread(target=work, name="worker-1")
t.start()
t.join()

Closer()  # объект тут же уничтожается, исключение из __del__ некуда поднять
print("дошли до конца, теперь исключение в главном потоке")
raise KeyError("упал главный поток")
```

```text title="Вывод"
threading.excepthook: поток worker-1: RuntimeError: упал поток
sys.unraisablehook: err_msg задан=True: OSError: не смог закрыть в __del__
дошли до конца, теперь исключение в главном потоке
sys.excepthook: KeyError: 'упал главный поток'
```

Блок специально завершается с кодом 1: последний `raise` и есть демонстрация `sys.excepthook`, процесс всё равно падает, хук только меняет то, как об этом сообщить. Порядок строк такой, как они срабатывали. Отличие версий: на 3.15 в `args.err_msg` для `__del__` уже лежит текст «Exception ignored while calling deallocator», на 3.12 там `None` (поэтому в блоке печатается только факт `err_msg задан`). Это наблюдение по прогону, в разделе «What's New» для 3.13–3.15 такое изменение отдельной строкой не найдено, поэтому на конкретную версию не опираются, а проверяют `err_msg is None`.

## Фоновая задача без потерь

Если main возвращается, не дождавшись фоновых задач, `asyncio.run` отменяет всё оставшееся. Работа оборвана посередине, а лог при этом может не дописаться.

```python
import asyncio

BATCHES = 3
BATCH_S = 0.05


async def flusher() -> None:
    written = 0
    try:
        for _ in range(BATCHES):
            await asyncio.sleep(BATCH_S)
            written += 1
    finally:
        print(f"flusher: записано пачек {written} из {BATCHES}")


async def main() -> None:
    asyncio.create_task(flusher(), name="flusher")
    await asyncio.sleep(0.07)
    print("main закончился")  # main возвращается, не дождавшись flusher


asyncio.run(main())
print("asyncio.run вернул управление")
```

```text title="Вывод"
main закончился
flusher: записано пачек 1 из 3
asyncio.run вернул управление
```

Записана одна пачка из трёх: `flusher` получил отмену на второй. Рецепт ниже делает три вещи. `spawn` хранит ссылку на задачу в множестве модуля и вешает done-callback: он убирает задачу из множества, пропускает отменённые и логирует упавшие вместе с именем задачи и трейсбеком (`exc_info=exc`). `shutdown(timeout)` ждёт задачи до таймаута, потом отменяет оставшиеся, дожидается их и возвращает имена отменённых, чтобы это можно было записать в лог. Отмену никто не глотает: `CancelledError` в `shutdown` не перехватывается, а задачи просто ждутся через `asyncio.wait`, который не бросает их исключения.

```python
import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

SHUTDOWN_TIMEOUT_S = 0.3  # сколько ждём задачи при выходе, прежде чем отменять
CANCEL_GRACE_S = 0.5  # сколько даём отменённым на уборку

log = logging.getLogger("app.tasks")

_tasks: set[asyncio.Task[Any]] = set()  # сильные ссылки: цикл хранит задачи только слабо


def _on_done(task: asyncio.Task[Any]) -> None:
    _tasks.discard(task)
    if task.cancelled():  # отмена - не ошибка, и task.exception() на ней бросил бы CancelledError
        return
    exc = task.exception()
    if exc is not None:
        log.error("фоновая задача %r упала", task.get_name(), exc_info=exc)


def spawn(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_on_done)
    return task


async def shutdown(timeout: float) -> list[str]:
    """Ждёт фоновые задачи до timeout, остальные отменяет и дожидается. Вернёт имена отменённых."""
    pending = set(_tasks)
    if not pending:
        return []
    _, not_done = await asyncio.wait(pending, timeout=timeout)
    for task in not_done:
        task.cancel()
    cancelled = sorted(task.get_name() for task in not_done)
    if not_done:
        _, stuck = await asyncio.wait(not_done, timeout=CANCEL_GRACE_S)
        for task in stuck:
            log.error("задача %r не ответила на отмену", task.get_name())
    return cancelled


async def flusher() -> None:
    for batch in range(3):
        await asyncio.sleep(0.05)
        log.info("flusher: записал пачку %d из 3", batch + 1)


async def exploder() -> None:
    await asyncio.sleep(0.01)
    raise RuntimeError("упала в фоне")


async def forever() -> None:
    try:
        await asyncio.sleep(3600)
    finally:
        log.info("forever: получил отмену и прибрался")


async def stubborn() -> None:
    """Плохая задача: глотает первую отмену. Починка - перевыбросить CancelledError."""
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        log.info("stubborn: проглотила отмену, живёт дальше")
        await asyncio.sleep(3600)  # вторую отмену (от asyncio.run при выходе) не глотаем


async def main() -> None:
    spawn(flusher(), name="flusher")
    spawn(stubborn(), name="stubborn")
    spawn(exploder(), name="exploder")
    spawn(forever(), name="forever")
    log.info("в работе задач: %d", len(_tasks))
    cancelled = await shutdown(SHUTDOWN_TIMEOUT_S)
    log.info("отменены при выходе: %s", cancelled)
    log.info("осталось ссылок: %d", len(_tasks))


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
asyncio.run(main())
```

```text title="Вывод"
INFO app.tasks: в работе задач: 4
ERROR app.tasks: фоновая задача 'exploder' упала
Traceback (most recent call last):
  File "spawn_demo.py", line 54, in exploder
    raise RuntimeError("упала в фоне")
RuntimeError: упала в фоне
INFO app.tasks: flusher: записал пачку 1 из 3
INFO app.tasks: flusher: записал пачку 2 из 3
INFO app.tasks: flusher: записал пачку 3 из 3
INFO app.tasks: forever: получил отмену и прибрался
INFO app.tasks: stubborn: проглотила отмену, живёт дальше
ERROR app.tasks: задача 'stubborn' не ответила на отмену
INFO app.tasks: отменены при выходе: ['forever', 'stubborn']
INFO app.tasks: осталось ссылок: 1
```

Вывод читается так. Упавшая `exploder` попала в лог сразу, с именем и трейсбеком, а не при сборке мусора. `flusher` успел записать все три пачки, потому что `shutdown` сначала ждёт. `forever` не уложилась в 0,3 с, получила отмену, прибралась в `finally`, и её имя вернулось из `shutdown`. Плохая `stubborn` проглотила отмену: `shutdown` ждал её ещё `CANCEL_GRACE_S`, записал ошибку «не ответила на отмену» и вернул её имя в том же списке отменённых, а в множестве осталась ссылка на неё (`осталось ссылок: 1`). Ни одна задача не потерялась молча: каждая либо дописала, либо названа в логе. Порядок двух строк отмены (`forever` и `stubborn`) может меняться между запусками и версиями: на 3.12 они идут в обратном порядке.

Три замечания по использованию. `shutdown` вызывают в `finally` главной корутины или по сигналу остановки, до выхода из `asyncio.run`.

Первое и главное: принудительно остановить задачу, которая глотает `CancelledError`, не может никакой код. `shutdown` лишь ждёт ограниченное время и честно называет такую задачу в логе. Дальше `asyncio.run` при выходе отменяет оставшиеся задачи ещё раз и ждёт их без ограничения: если задача глотает и эту отмену, процесс повиснет. В модели `stubborn` проглатывает отмену один раз, поэтому блок завершается; задача, которая глотает каждую, подвесила бы его навсегда. Лечится это только починкой самой задачи: перехватывать `CancelledError` ради уборки и перевыбрасывать ([[#Проглоченная отмена]]). Крайняя мера для процесса, который уже нельзя спасти, это `os._exit`, но он не вызывает `finally` и не сбрасывает буферы.

Второе: если задачи короткие и однородные, проще и надёжнее `TaskGroup`: он сам хранит ссылки и собирает ошибки, но падение одной задачи отменяет остальные (раздел «Падение одной задачи отменяет соседей» заметки [[Ошибки и отмена в asyncio]]). `spawn` нужен там, где задача должна падать независимо.

Третье: `spawn` копирует контекст на момент вызова, поэтому `trace_id` надо установить до `spawn`, а не после (подробности в следующем разделе).

## trace_id в каждой строке лога

Когда параллельно идут десятки запросов, строки лога перемешиваются и непонятно, какая чья. Решение: идентификатор запроса в `ContextVar`, а в каждую строку его подставляет `logging.Filter`. `ContextVar` хранит значение для каждой задачи отдельно, а задача, созданная из другой, получает копию контекста родителя и поэтому наследует `trace_id` (подробнее: раздел «`ContextVar` в двух задачах» заметки [[asyncio на практике]]). Фильтр вешается на handler, а не на логгер: фильтры логгера не применяются к записям дочерних логгеров.

```python
import asyncio
import logging
from contextvars import ContextVar

trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


class TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id.get()
        return True


log = logging.getLogger("app.request")


async def child_step(n: int) -> None:
    await asyncio.sleep(0)
    log.info("шаг %d во вложенной задаче", n)


async def handle_request(request_id: str) -> None:
    trace_id.set(request_id)  # меняет контекст только текущей задачи
    log.info("запрос принят")
    await asyncio.sleep(0)
    await asyncio.create_task(child_step(1), name=f"child-{request_id}")  # копия контекста родителя
    log.info("запрос обработан")


async def main() -> None:
    log.info("до запросов")
    await asyncio.gather(handle_request("req-A"), handle_request("req-B"))
    log.info("после запросов, в главной задаче trace_id=%s", trace_id.get())


handler = logging.StreamHandler()
handler.addFilter(TraceIdFilter())  # фильтр на handler: он сработает и для записей дочерних логгеров
handler.setFormatter(logging.Formatter("%(trace_id)-6s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler])
asyncio.run(main())
```

```text title="Вывод"
-      до запросов
req-A  запрос принят
req-B  запрос принят
req-A  шаг 1 во вложенной задаче
req-B  шаг 1 во вложенной задаче
req-A  запрос обработан
req-B  запрос обработан
-      после запросов, в главной задаче trace_id=-
```

Строки двух запросов чередуются, но у каждой стоит свой `req-A` или `req-B`, включая записи из вложенной задачи: она унаследовала значение. В главной задаче после всего стоит `-`: `trace_id.set` внутри `handle_request` изменил контекст только той задачи. Порядок чередования здесь детерминирован (паузы `sleep(0)`), в реальном сервисе он будет другим, а `trace_id` останется верным. Теперь `grep req-A` собирает цепочку одного запроса. Для сервиса идентификатор берут из входящего заголовка `traceparent` стандарта W3C Trace Context, а при его отсутствии генерируют; это идея, без привязки к конкретному фреймворку. Чтобы `spawn` из предыдущего раздела тоже писал с `trace_id`, достаточно подключить тот же фильтр: `create_task` внутри `spawn` скопирует контекст вызвавшего. Копия делается в момент создания: задача, созданная до `trace_id.set(...)`, значения не увидит и будет писать `-`.

## Разбор результатов gather

`gather(..., return_exceptions=True)` не бросает ошибки, а кладёт их в список рядом с результатами. Если список не разобрать, ошибки теряются так же тихо, как у брошенной задачи. Правило: после такого `gather` каждый элемент проверяется, ошибки логируются с трейсбеком, а вызывающий код получает успехи и ошибки раздельно и решает, что делать (повторить, пропустить, упасть).

```python
import asyncio
import logging
from collections.abc import Awaitable, Sequence
from typing import TypeVar

T = TypeVar("T")

log = logging.getLogger("app.gather")


async def gather_checked(
    labels: Sequence[str], awaitables: Sequence[Awaitable[T]]
) -> tuple[list[tuple[str, T]], list[tuple[str, BaseException]]]:
    """gather(return_exceptions=True) с разбором: успехи отдельно, ошибки (и отменённые) отдельно.

    Отмена самого gather снаружи бросает CancelledError из await ниже и сюда не попадает.
    CancelledError в списке значит, что отменили дочернюю задачу: это данные, а не отмена вызывающего.
    """
    results = await asyncio.gather(*awaitables, return_exceptions=True)
    ok: list[tuple[str, T]] = []
    failed: list[tuple[str, BaseException]] = []
    for label, result in zip(labels, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            log.warning("%s: дочерняя задача отменена", label)
            failed.append((label, result))
        elif isinstance(result, BaseException):
            log.error("%s: не удалось", label, exc_info=result)
            failed.append((label, result))
        else:
            ok.append((label, result))
    return ok, failed


async def fetch(n: int) -> int:
    await asyncio.sleep(0)
    if n == 2:
        raise asyncio.CancelledError  # имитация: дочернюю задачу отменил кто-то другой
    if n % 2:
        raise ValueError(f"нечётный номер {n}")
    return n * 10


async def main() -> None:
    labels = [f"item-{n}" for n in range(4)]
    ok, failed = await gather_checked(labels, [fetch(n) for n in range(4)])
    print("успехи:", ok)
    print("ошибки:", [(label, type(exc).__name__) for label, exc in failed])


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
asyncio.run(main())
```

```text title="Вывод"
ERROR app.gather: item-1: не удалось
Traceback (most recent call last):
  File "gather_check.py", line 39, in fetch
    raise ValueError(f"нечётный номер {n}")
ValueError: нечётный номер 1
WARNING app.gather: item-2: дочерняя задача отменена
ERROR app.gather: item-3: не удалось
Traceback (most recent call last):
  File "gather_check.py", line 39, in fetch
    raise ValueError(f"нечётный номер {n}")
ValueError: нечётный номер 3
успехи: [('item-0', 0)]
ошибки: [('item-1', 'ValueError'), ('item-2', 'CancelledError'), ('item-3', 'ValueError')]
```

Что делает `gather_checked`. Результаты соотносятся с метками через `zip(..., strict=True)`: порядок результатов совпадает с порядком аргументов. Каждая ошибка логируется с `exc_info=result`, поэтому трейсбек остаётся: просто `log.error("%s", result)` оставил бы одну строку без причины (раздел «log.exception и exc_info» заметки [[Логирование]]). Цикл не прерывается ни на одном элементе, так что в логе есть все ошибки, а в ответе все успехи.

Отдельно про `CancelledError`. Если отменят саму задачу, которая ждёт `gather`, исключение вылетит из `await asyncio.gather(...)` и до цикла разбора не дойдёт: вызывающий и так увидит свою отмену, перехватывать её не нужно. А вот `CancelledError` внутри списка означает, что отменили дочернюю задачу (в модели её имитирует `raise asyncio.CancelledError` в `fetch`). Это данные: она логируется как предупреждение и попадает в «не удалось» вместе с остальными, чтобы не потерять другие результаты на полпути. Перевыбрасывать её из цикла означало бы объявить отменённым вызывающего, которого никто не отменял. Если какую-то ошибку нужно повторить, повторять нужно только идемпотентные операции.

## ExceptionGroup и add_note

Группа исключений из `TaskGroup` (и из `except*`) в логе легко теряется. `str(group)` даёт одну строку «2 sub-exceptions» без причин, поэтому `log.error("%s", group)` почти бесполезен. Нужен `exc_info=group`: тогда `logging` печатает группу целиком, с трейсбеком каждого вложенного исключения. Контекст границы (какой пользователь, какой запрос) добавляют через `exc.add_note(...)`, примечания печатаются под каждым исключением. Нужные возможности (`ExceptionGroup`, `except*`, `add_note`) появились в Python 3.11 (PEP 654 и PEP 678, «What's New in 3.11»).

```python
import asyncio
import logging

log = logging.getLogger("app.group")


async def fail(kind: type[Exception], text: str) -> None:
    await asyncio.sleep(0)
    raise kind(text)


async def load_profile(user_id: int) -> None:
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(fail(ValueError, "плохой email"), name="email")
            tg.create_task(fail(KeyError, "phone"), name="phone")
    except* (ValueError, KeyError) as group:
        for exc in group.exceptions:
            exc.add_note(f"при загрузке профиля user_id={user_id}")  # контекст на границе
        raise


async def main() -> None:
    try:
        await load_profile(42)
    except ExceptionGroup as group:
        log.error("потеряны детали: %s", group)  # str() группы - одна строка без причин
        log.error("целиком:", exc_info=group)  # traceback группы включает каждое вложенное исключение


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
asyncio.run(main())
```

```text title="Вывод"
ERROR app.group: потеряны детали: unhandled errors in a TaskGroup (2 sub-exceptions)
ERROR app.group: целиком:
  + Exception Group Traceback (most recent call last):
  |   File "exc_group.py", line 25, in main
  |     await load_profile(42)
  |   File "exc_group.py", line 14, in load_profile
  |     async with asyncio.TaskGroup() as tg:
  |                ~~~~~~~~~~~~~~~~~^^
  |   ...
  | ExceptionGroup: unhandled errors in a TaskGroup (2 sub-exceptions)
  +-+---------------- 1 ----------------
    | Traceback (most recent call last):
    |   File "exc_group.py", line 9, in fail
    |     raise kind(text)
    | ValueError: плохой email
    | при загрузке профиля user_id=42
    +---------------- 2 ----------------
    | Traceback (most recent call last):
    |   File "exc_group.py", line 9, in fail
    |     raise kind(text)
    | KeyError: 'phone'
    | при загрузке профиля user_id=42
    +------------------------------------
```

Первая строка потеряла причины, вторая показала обе: `ValueError` и `KeyError`, каждая с примечанием `user_id=42`. `add_note` вызывается на вложенных исключениях, а группа после этого перевыбрасывается (`raise` в `except*`). На 3.12 блок идёт без изменений, отличаются только пути и подчёркивания `^^^` в стеке стандартной библиотеки. Как сами группы появляются в `TaskGroup`: раздел «`ExceptionGroup` во вложенных `TaskGroup`» заметки [[Ошибки и отмена в asyncio]].

## Лог, который не дошёл

Строка, которую программа «напечатала», может не дойти до лога, если процесс умер раньше, чем сбросился буфер. Виноват `print` (и вообще `sys.stdout`): когда вывод идёт не в терминал, а в пайп или файл (лог контейнера, перенаправление), он буферизуется блоками и ждёт сброса. Процесс, убитый без штатного завершения, уносит буфер с собой. `logging.StreamHandler` сбрасывает поток после каждой записи сам, поэтому записи через него теряются только если процесс упал до самого вызова логгера. Исключение в потоке сюда не относится: это отдельная потеря, её ловит `threading.excepthook` ([[#Хуки процесса и потока]]). Модель: дочерний процесс печатает строку и аварийно выходит, родитель читает пайп.

```python
import subprocess
import sys

CHILD = """
import os
print("строка из дочернего процесса")
os._exit(1)  # аварийный выход без сброса буферов, как у убитого контейнера
"""


def run(*extra: str) -> None:
    proc = subprocess.run(
        [sys.executable, *extra, "-c", CHILD], capture_output=True, text=True, timeout=30
    )
    print(f"python {' '.join(extra) or '(без флагов)':<12} stdout={proc.stdout.strip()!r}")


run()
run("-u")
```

```text title="Вывод"
python (без флагов) stdout=''
python -u           stdout='строка из дочернего процесса'
```

Без флагов строка пропала: `stdout` в пайпе ждёт сброса буфера, которого не случилось. С `-u` строка дошла. Лечение: `PYTHONUNBUFFERED=1` (в контейнере стоит включать его всегда), `python -u` или `print(..., flush=True)` для отдельных строк. Для логов из дочерних процессов записи собирают в одну очередь (`logging.handlers.QueueHandler` и `QueueListener`) вместо записи в один файл из нескольких процессов. Если процесс завис и логов нет, смотрят стеки: `faulthandler.dump_traceback_later` и `asyncio.all_tasks()` описаны в разделе «Как искать зависания» страницы [[Выбор примитива синхронизации]].

## Что спросят дальше

- **Что значит «Task exception was never retrieved» и почему оно приходит поздно?** Задача упала, а исключение никто не забрал (`await`, `result()`, `exception()`). Сообщение печатает деструктор задачи при сборке, поэтому оно приходит, когда исчезла последняя ссылка, а не когда задача упала.
- **Почему фоновая задача может пропасть без следа?** Цикл держит задачи только слабыми ссылками, сборщик мусора вправе забрать недоделанную. Нужна сильная ссылка: множество в модуле или `TaskGroup`.
- **Как не потерять ошибку фоновой задачи?** Done-callback, который пропускает отменённые, читает `task.exception()` и логирует с `exc_info`. Плюс `shutdown` с ожиданием и отменой при выходе.
- **Что делает `python -X dev` в asyncio?** Включает режим отладки: стек создания задач и корутин в предупреждениях, лог колбэков дольше 100 мс, проверки потока. Заодно показывает `ResourceWarning`. Цена: замедление, на проде не включают.
- **Чем `loop.set_exception_handler` отличается от `sys.excepthook`?** Первый ловит то, что цикл не смог отдать задаче или колбэку. Второй ловит исключение, дошедшее до верха главного потока. Исключения задач asyncio до `sys.excepthook` не доходят.
- **Что делать с `gather(return_exceptions=True)`?** Разобрать список: ошибки логировать с `exc_info`, отмену дочерней задачи залогировать как предупреждение и вернуть среди неудач, успехи и ошибки вернуть раздельно. Отмена самого вызывающего вылетает из `await gather(...)` и в список не попадает.
- **Как залогировать `ExceptionGroup`?** `log.error(..., exc_info=group)`, не `%s`: так видны все вложенные исключения. Контекст границы кладут через `add_note`.
- **Как связать логи одного запроса?** `ContextVar` с `trace_id` и `logging.Filter` на handler; `create_task` копирует контекст, дочерние задачи наследуют значение.
