---
title: "Python: потоки, процессы, asyncio, GIL и GC"
---

Конспект к собеседованию по конкурентности в Python: какие бывают виды параллелизма и какой когда брать, что такое GIL и почему он не делает код потокобезопасным, как устроена сборка мусора, как синхронизировать потоки и процессы и что происходит под капотом у `async`/`await`: корутина, цикл событий, задача, Future.
Рядом: [[Python-протоколы]] (генераторы, на которых исторически выросли корутины) · [[Python-классы]] · [[Redis#Техника: распределённая блокировка|распределённая блокировка]] · [[Блокировки|блокировки в PostgreSQL]].
Разделы: [[#Виды параллелизма]] · [[#GIL]] · [[#Сборка мусора (GC)]] · [[#Синхронизация потоков]] · [[#Синхронизация процессов]] · [[#Async под капотом]] · [[#asyncio на практике]] · [[#Что спросят дальше]]

**Суть.** Конкурентность (concurrency) — умение вести несколько задач одновременно, переключаясь между ними; параллелизм (parallelism) — выполнять их буквально в один момент на разных ядрах. В обычном CPython GIL (global interpreter lock, глобальная блокировка интерпретатора) не даёт двум потокам одновременно выполнять Python-код, поэтому потоки и asyncio дают конкурентность, но не параллелизм: они ускоряют ожидание (сеть, диск, база), а не вычисления. Для вычислений нужны процессы, C-расширения, которые отпускают GIL (numpy), или новая сборка Python без GIL. Правило выбора: ждём ввод-вывод на многих соединениях — asyncio; ждём ввод-вывод, но библиотеки блокирующие — потоки; считаем — процессы.

## Виды параллелизма

| Модель | Модуль | Параллельно на ядрах? | Память | Цена переключения | Когда брать |
| --- | --- | --- | --- | --- | --- |
| Потоки (threads) | `threading`, `ThreadPoolExecutor` | нет, кроме ожидания ввода-вывода и C-кода | общая | переключает ОС, дорого на тысячах | блокирующий ввод-вывод: `requests`, драйверы БД без async, файлы; десятки–сотни задач |
| Процессы (processes) | `multiprocessing`, `ProcessPoolExecutor` | да | у каждого своя, данные передаются через pickle | дорогой запуск, дорогая передача данных | вычисления (CPU-bound): парсинг, сжатие, картинки, математика на чистом Python |
| asyncio | `asyncio` | нет, один поток | общая | дёшево, переключение только на `await` | тысячи одновременных соединений: HTTP-сервер, клиенты API, вебсокеты |
| Субинтерпретаторы (subinterpreters) | `concurrent.interpreters`, `InterpreterPoolExecutor` (3.14+) | да, у каждого свой GIL | раздельная, в одном процессе | дешевле процессов | вычисления без расходов на процессы; библиотеки ещё догоняют |
| Сборка без GIL (free-threaded) | `python3.14t` | да | общая | как у потоков | вычисления в потоках с общей памятью; официально поддерживается с 3.14, но не по умолчанию |

Замер на 4 ядрах, Python 3.15, четыре одинаковые вычислительные задачи (цикл с умножением на чистом Python):

| Как запускали | Время |
| --- | --- |
| последовательно | 0,64 с |
| `ThreadPoolExecutor(4)` | 0,65 с — ускорения нет, мешает GIL |
| `ProcessPoolExecutor(4)` | 0,23 с |

Так же замер можно повторить самому (числа ниже получены на этой машине и округлены: на другой будут другие, важно соотношение):

```python
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor


def cpu(n: int) -> int:
    total = 0
    for i in range(n):
        total += i * i
    return total


def run(name, pool_cls):
    start = time.perf_counter()
    if pool_cls is None:
        list(map(cpu, [5_000_000] * 4))
    else:
        with pool_cls(4) as pool:
            list(pool.map(cpu, [5_000_000] * 4))
    print(f"{name:8} {time.perf_counter() - start:.1f} с")


if __name__ == "__main__":
    run("подряд", None)
    run("потоки", ThreadPoolExecutor)
    run("процессы", ProcessPoolExecutor)
```

```text title="Вывод"
подряд   0.7 с
потоки   0.7 с
процессы 0.2 с
```

Потоки не быстрее последовательного запуска: Python-код в каждый момент исполняет только один из них.

`concurrent.futures` даёт один интерфейс для потоков и процессов: `submit` возвращает `Future`, `map` — результаты по порядку, `as_completed` — по мере готовности.

```python
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import time


def fetch(url: str) -> str:
    time.sleep(0.1)  # здесь — requests.get(url): ждём сеть, GIL отпущен
    return url


def cpu(n: int) -> int:
    return sum(i * i for i in range(n))


if __name__ == "__main__":  # обязательно для процессов, см. ниже
    with ThreadPoolExecutor(max_workers=10) as pool:
        pages = list(pool.map(fetch, [f"https://example.com/{i}" for i in range(10)]))
    with ProcessPoolExecutor() as pool:  # по умолчанию — по процессу на ядро
        sums = list(pool.map(cpu, [1_000_000] * 4))
    print(len(pages), len(sums))
```

Особенности процессов, о которых спрашивают:

- **Способ запуска (start method).** `fork` копирует родительский процесс целиком, быстро, но опасно: если в родителе были потоки, в потомке окажутся захваченные чужими потоками блокировки, которые некому отпустить, — взаимная блокировка (deadlock). `spawn` запускает чистый интерпретатор и импортирует модуль заново: медленнее, зато безопасно. `forkserver` — компромисс: отдельный чистый процесс-сервер, от которого делаются `fork`. По умолчанию: macOS и Windows — `spawn`, Linux с Python 3.14 — `forkserver` (раньше был `fork`).
- **`if __name__ == "__main__":`** обязателен при `spawn` и `forkserver`: потомок импортирует главный модуль, и без этой проверки снова начнёт создавать процессы.
- **Аргументы и результаты сериализуются через pickle.** Лямбды, вложенные функции, открытые соединения и файлы передать нельзя; большие объекты передавать дорого. Пересылать нужно мало, считать много.

## GIL

**Что это.** GIL — мьютекс (mutex, блокировка взаимного исключения) внутри CPython: Python-байткод в каждый момент выполняет только тот поток, который держит GIL. Другие потоки ждут.

**Зачем он нужен.** Каждый объект в CPython хранит счётчик ссылок (см. [[#Сборка мусора (GC)]]), и он меняется при каждом присваивании. Без общей блокировки счётчики пришлось бы менять атомарно или защищать отдельными блокировками, а это замедляет однопоточный код. Ещё GIL упрощает C-расширения: их авторы могут не думать о потоках. Цена — потоки не выполняют Python-код параллельно.

**Когда GIL отпускается:**

- на блокирующем вводе-выводе: чтение из сокета, файла, `time.sleep`, ожидание ответа базы. Поэтому потоки отлично ускоряют сетевой код;
- в C-расширениях, которые отпускают его явно: numpy, `hashlib` и `zlib` на больших данных, драйверы БД;
- принудительно: если другой поток ждёт GIL дольше интервала переключения (`sys.getswitchinterval()`, 5 мс), текущий поток просят отдать GIL в ближайшей безопасной точке.

**GIL не делает код потокобезопасным.** Он гарантирует целостность внутренних структур интерпретатора (список не сломается от двух `append`), но не вашей логики. `x += 1` — это несколько операций байткода: прочитать, сложить, записать; поток могут переключить между ними. Реальнее всего гонка (race condition) проявляется в схеме «проверить, потом действовать», где между проверкой и действием есть ввод-вывод:

```python
import contextlib
import threading
import time

balance = 100
start = threading.Barrier(2)  # оба потока стартуют одновременно


def withdraw(amount: int, guard) -> None:
    global balance
    start.wait()
    with guard:
        if balance >= amount:      # проверили
            time.sleep(0.001)      # запрос в БД, логирование — GIL отпущен
            balance -= amount      # действуем по устаревшей проверке


def run(guard) -> int:
    global balance
    balance = 100
    threads = [threading.Thread(target=withdraw, args=(100, guard)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return balance


print("без блокировки:", run(contextlib.nullcontext()))
print("с Lock:        ", run(threading.Lock()))
```

```text title="Вывод"
без блокировки: -100
с Lock:         0
```

`Barrier` нужен, чтобы оба потока стартовали вместе, иначе первый может успеть закончить до запуска второго. Без блокировки оба прошли проверку `balance >= 100`, пока первый «ходил в базу», и баланс ушёл в минус. Под общей `Lock` вся пара «проверил и списал» выполняется целиком, и второй поток видит уже 0. (`time.sleep(0)` здесь гонку не воспроизвёл: GIL он отпускает, но возвращается раньше, чем второй поток успевает дойти до проверки; нужна пауза заметной длины, как у настоящего запроса.)

Лечится блокировкой вокруг всей пары «проверить и действовать» (см. [[#Синхронизация потоков]]), а если состояние лежит в базе — средствами базы, см. [[Блокировки]].

Отдельные операции со встроенными типами (`list.append`, `dict[key] = value`, `queue.put`) в CPython атомарны. Но это деталь реализации: опираться на неё в логике из нескольких шагов нельзя.

**Будущее без GIL.**

- **PEP 703, сборка без GIL (free-threaded)**: в 3.13 экспериментальная, с 3.14 официально поддерживается (PEP 779), но ставится отдельно (`python3.14t`) и пока не по умолчанию. Вместо GIL — блокировки на уровне объектов, «смещённый» подсчёт ссылок (biased reference counting: поток-владелец меняет счётчик без атомарных операций) и аллокатор mimalloc. Однопоточный код в такой сборке медленнее на несколько процентов, а C-расширения должны явно объявить поддержку, иначе GIL включится обратно при импорте.
- **Субинтерпретаторы со своим GIL** (PEP 684 в 3.12, модуль `concurrent.interpreters` в 3.14): несколько изолированных интерпретаторов в одном процессе, которые работают параллельно и обмениваются данными через очереди.

## Сборка мусора (GC)

В CPython память освобождают два механизма.

**1. Подсчёт ссылок (reference counting) — основной.** У каждого объекта есть счётчик: сколько переменных, контейнеров и других объектов на него ссылается. Присвоили — счётчик +1, переменная вышла из области видимости или её переназначили — −1. Дошёл до нуля — объект удаляется **сразу**, детерминированно. Поэтому в CPython файл, открытый без `with`, обычно закрывается при выходе из функции (но в PyPy — нет, и полагаться на это нельзя). Посмотреть счётчик: `sys.getrefcount(obj)` (покажет на единицу больше: аргумент функции — тоже ссылка).

**2. Циклический сборщик (`gc`) — для циклических ссылок.** Объекты, ссылающиеся друг на друга (`a.b = b; b.a = a`, узел дерева со ссылкой на родителя, исключение, которое держит кадр стека, который держит это исключение), никогда не получат нулевой счётчик. Их находит отдельный сборщик:

- он следит только за **контейнерами** (списки, словари, экземпляры классов), потому что числа и строки не могут ссылаться на другие объекты и цикл не образуют;
- для каждого контейнера считает, сколько ссылок на него приходит **изнутри** проверяемой группы; если все ссылки внутренние, снаружи до объекта не добраться;
- от объектов, на которые есть внешние ссылки, проходит по их ссылкам и помечает всё достижимое как живое; остальное — мусор, его финализируют (`__del__`, с Python 3.4 работает и для циклов, PEP 442) и освобождают.

Сборщик **поколенческий** (generational): новые объекты попадают в поколение 0; пережившие сборку переходят в следующее. Идея — большинство объектов умирают молодыми, поэтому часто проверяют только молодых. Сборка поколения 0 запускается, когда число созданных контейнеров минус удалённых превышает порог (`gc.get_threshold()`: 700 до Python 3.12, 2000 с 3.13), старшие поколения — реже. Сборка останавливает программу, пока идёт, поэтому на огромных кучах объектов бывают паузы.

Подсчёт ссылок и цикл, который счётчиком не собрать (`weakref.finalize` печатает сообщение в момент удаления объекта):

```python
import gc
import sys
import weakref


class Node:
    pass


a = Node()
print(sys.getrefcount(a))  # a + аргумент getrefcount
b = a
print(sys.getrefcount(a))
del b
print(sys.getrefcount(a))
del a

x = Node()
weakref.finalize(x, print, "простой объект удалён")
del x
print("после del x")

y, z = Node(), Node()
y.other, z.other = z, y
weakref.finalize(y, print, "цикл собран")
del y, z
print("после del y, z")
gc.collect()
print("после gc.collect()")
```

```text title="Вывод"
2
3
2
простой объект удалён
после del x
после del y, z
цикл собран
после gc.collect()
```

Первая строка — `2`, а не `1`: аргумент `getrefcount` тоже ссылка. Простой объект удалён в момент `del x`, до следующей строки. Пара `y` и `z` ссылается друг на друга, поэтому после `del` счётчики не нули, и объекты живут до `gc.collect()` (или до очередной автоматической сборки).

Практика:

- **`gc.freeze()` перед `fork`** в серверах с предварительным запуском воркеров (prefork: gunicorn, uWSGI). После `fork` память делится между процессами, пока её не записали (copy-on-write). Но сборщик, обходя объекты, пишет в их заголовки, и страницы копируются в каждый воркер. `freeze()` переносит все текущие объекты в «вечное» поколение, которое сборщик не трогает. Instagram так экономил гигабайты на сервер.
- **`weakref`** — слабая ссылка, которая не увеличивает счётчик. Для кэшей (`WeakValueDictionary`), подписчиков событий, ссылки «ребёнок → родитель», чтобы не создавать циклы и не держать объекты вечно.
- **Утечки памяти в Python** — это почти всегда не баг сборщика, а объекты, на которые кто-то ссылается: глобальный словарь-кэш без ограничения, `lru_cache` на методе, список «для отладки», сохранённое исключение с его traceback и всеми локальными переменными кадров. Искать: `tracemalloc` (кто выделил память, сравнение снимков), `gc.get_referrers`, objgraph.
- **Процесс не отдаёт память ОС** после удаления объектов: малые объекты (до 512 байт) аллоцируются в аренах pymalloc, и арена возвращается системе, только если пуста целиком. Фрагментация держит RSS (resident set size, реально занятую процессом память) высоким. Для воркеров с пиками памяти ставят перезапуск после N задач (`max_requests` в gunicorn, `max_tasks_per_child` в Celery).

## Синхронизация потоков

Примитивы модуля `threading`:

| Примитив | Что делает | Когда |
| --- | --- | --- |
| `Lock` | один владелец; повторный захват тем же потоком — deadlock | защитить критическую секцию |
| `RLock` | реентерабельный: тот же поток может захватить повторно, нужно столько же освобождений | рекурсия, метод под блокировкой вызывает другой такой же метод |
| `Semaphore(n)` / `BoundedSemaphore(n)` | не больше n владельцев одновременно; `Bounded` ругается на лишний `release` | ограничить число одновременных запросов к внешнему API |
| `Event` | флаг: `set`, `clear`, `wait` | сигнал «остановиться», «данные готовы» |
| `Condition` | блокировка + ожидание условия (`wait_for(predicate)`, `notify`) | своя очередь, «ждать, пока в буфере есть место» |
| `Barrier(n)` | n потоков ждут друг друга в одной точке | фазы вычислений, тесты на гонки |
| `queue.Queue` | потокобезопасная очередь с блокирующими `get`/`put` и `maxsize` | производитель–потребитель; почти всегда лучший выбор |
| `threading.local()` / `contextvars` | у каждого потока (контекста) своё значение | соединение на поток, id запроса для логов |

Блокировку берут через `with`, а не парой `acquire()`/`release()`: исключение внутри не оставит её захваченной.

```python
import threading


class Account:
    def __init__(self, balance: int) -> None:
        self.balance = balance
        self.lock = threading.Lock()


def transfer(src: Account, dst: Account, amount: int) -> bool:
    # Захватываем всегда в одном порядке (по id), иначе встречный перевод даст deadlock
    first, second = sorted((src, dst), key=id)
    with first.lock, second.lock:
        if src.balance < amount:
            return False
        src.balance -= amount
        dst.balance += amount
        return True


a, b = Account(100), Account(0)
threads = [threading.Thread(target=transfer, args=(a, b, 100)) for _ in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print(a.balance, b.balance)  # 0 100 — прошёл ровно один перевод
```

**Взаимная блокировка (deadlock)** — два потока держат по блокировке и ждут чужую. Классика — перевод A→B и B→A одновременно. Лечение: захватывать блокировки всегда в одном порядке (как выше), держать их как можно меньше, не вызывать под блокировкой внешний код, ставить таймаут (`lock.acquire(timeout=5)`), а лучше — вообще не делить состояние и передавать данные через очередь.

**Производитель–потребитель через `queue.Queue`:**

```python
import queue
import threading

STOP = object()  # маркер завершения (sentinel)


def worker(tasks: queue.Queue, results: list[int]) -> None:
    while True:
        item = tasks.get()
        if item is STOP:
            break
        results.append(item * 2)  # отдельный append атомарен


tasks: queue.Queue = queue.Queue(maxsize=100)  # maxsize даёт противодавление (backpressure)
results: list[int] = []
workers = [threading.Thread(target=worker, args=(tasks, results)) for _ in range(4)]
for w in workers:
    w.start()
for i in range(10):
    tasks.put(i)  # если очередь полна — производитель ждёт
for _ in workers:
    tasks.put(STOP)
for w in workers:
    w.join()
print(sorted(results))
```

`maxsize` важен: без него быстрый производитель заполнит память, пока медленные потребители не успевают. Это противодавление (backpressure) — тот же принцип, что у консьюмеров [[Kafka]].

## Синхронизация процессов

У процессов нет общей памяти, поэтому сначала вопрос «как передать данные», потом — «как синхронизировать».

| Средство | Что это | Цена |
| --- | --- | --- |
| `multiprocessing.Queue`, `Pipe` | передача объектов через pickle по каналу ОС | сериализация каждого сообщения |
| `Value`, `Array` | число или массив C-типов в разделяемой памяти, со встроенной блокировкой (`get_lock()`) | дёшево, но только простые типы |
| `shared_memory.SharedMemory` | именованный блок общей памяти, поверх него — numpy-массив | без копирования; синхронизация — ваша забота |
| `Manager()` | отдельный процесс-сервер хранит `dict`/`list`, остальные обращаются через прокси | каждое обращение — межпроцессный вызов, медленно |
| `multiprocessing.Lock`, `RLock`, `Semaphore`, `Event`, `Condition`, `Barrier` | те же примитивы, но на семафорах ОС | работают только между процессами одного «семейства» (переданы при создании) |

```python
from multiprocessing import Process, Value


def add(counter, n: int) -> None:
    for _ in range(n):
        with counter.get_lock():  # += на разделяемой памяти не атомарен
            counter.value += 1


if __name__ == "__main__":
    counter = Value("i", 0)
    procs = [Process(target=add, args=(counter, 1000)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    print(counter.value)  # 4000
```

**Процессы, которые не родственники**, — воркеры gunicorn, поды в Kubernetes, разные серверы — примитивами `multiprocessing` не синхронизировать. Нужна внешняя точка согласования:

- файловая блокировка (`fcntl.flock`) — только в пределах одной машины;
- блокировка в базе: `SELECT ... FOR UPDATE`, advisory lock, ограничение `UNIQUE` — см. [[Блокировки]];
- блокировка в Redis с TTL и fencing token — см. [[Redis#Техника: распределённая блокировка]];
- очередь сообщений вместо общего состояния — [[Kafka]], [[RabbitMQ]], [[Celery]].

Частая ошибка: кэш в памяти процесса или `threading.Lock` в веб-приложении, которое запущено в нескольких воркерах. У каждого воркера своя копия: блокировка ничего не блокирует, кэши расходятся.

## Async под капотом

### Зачем вообще async

Поток на каждое соединение — простая модель, но на десяти тысячах соединений дорогая: у каждого потока свой стек (резервируются мегабайты адресного пространства), а переключает их ОС с полным сохранением контекста. При этом почти всё время потоки просто ждут сеть.

Идея async: один поток, неблокирующие сокеты и системный вызов, который ждёт сразу на всех сокетах и возвращает те, что готовы (`epoll` в Linux, `kqueue` в macOS; в Python — модуль `selectors`). Когда задаче нужно ждать, она отдаёт управление, и поток занимается другими задачами. Переключение — это просто вызов функции в том же потоке, без участия ОС.

### Четыре понятия

- **Корутина (coroutine).** Функция, объявленная через `async def`, — корутинная функция; её вызов ничего не выполняет, а возвращает объект-корутину. Корутина умеет приостанавливаться на `await` и продолжать с того же места. Устроена как генератор (см. [[Python-протоколы#yield from, send, throw, close]]): её «прокручивают» методом `send(None)`, а на точке ожидания она отдаёт наружу объект, которого ждёт.
- **Future.** Объект-обещание: результата ещё нет, но будет. Хранит состояние (ожидает, готов, отменён), результат или исключение и список колбэков, которые вызовутся при завершении. `await future` приостанавливает корутину, пока кто-то не вызовет `future.set_result(...)`. В прикладном коде напрямую почти не встречается: на нём строятся библиотеки.
- **Task.** Наследник Future, который ведёт корутину: вызывает её `send`, пока она не завершится. Если корутина остановилась на Future, задача подписывается на него и продолжит корутину, когда Future станет готов. Результат задачи — то, что вернула корутина. Создаётся через `asyncio.create_task(coro)` или `TaskGroup.create_task(coro)`, и с этого момента корутина выполняется **конкурентно**, не дожидаясь `await`.
- **Цикл событий (event loop).** Планировщик в одном потоке. В бесконечном цикле: 1) выполнить все готовые колбэки из очереди; 2) посчитать, сколько можно ждать до ближайшего таймера; 3) спросить ОС, какие сокеты готовы, с этим таймаутом; 4) поставить колбэки готовых сокетов и истёкших таймеров в очередь. «Шаг задачи» — это просто колбэк в этой очереди.

`await x` работает с любым awaitable-объектом: корутиной, Task, Future или объектом с методом `__await__`. `await` на корутине — как `yield from`: выполнение идёт внутрь, вплоть до самого нижнего Future, который отдаётся наверх, через все корутины, до Task.

### Цикл событий своими руками

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

## asyncio на практике

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

### `gather` и `TaskGroup`: что с остальными задачами, когда одна упала

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

### `asyncio.timeout`

```python
import asyncio


async def main() -> None:
    try:
        async with asyncio.timeout(0.1):
            await asyncio.sleep(1)
    except TimeoutError as e:
        print(repr(e))


asyncio.run(main())
```

```text title="Вывод"
TimeoutError()
```

Внутри блока задача отменена (`CancelledError` на `await`), а снаружи `async with` превратил отмену в обычный `TimeoutError`.

Ловушки:

- **Забытый `await`.** `fetch(1)` без `await` создаёт корутину и ничего не делает; Python предупредит `RuntimeWarning: coroutine ... was never awaited`, но только когда объект корутины удаляется: сразу, если ссылки на неё нет (как ниже), и позже, если она где-то лежит. Пример в подразделе «Забытый `await`».
- **Задача «запустил и забыл».** Цикл событий хранит на задачи только слабые ссылки: `asyncio.create_task(send_email())` без сохранения результата может быть собрана сборщиком мусора посреди работы. Держите ссылки (множество задач с `add_done_callback(tasks.discard)`) или используйте `TaskGroup`.
- **Отмена.** `task.cancel()` бросает `CancelledError` внутрь корутины на текущем `await`. Её нужно пробрасывать дальше (или пересоздавать после очистки), а не глотать: `except Exception` её не ловит, а вот `except BaseException` и голый `except:` ловят. Защитить кусок от отмены снаружи — `asyncio.shield`. Пример ниже, в подразделе «Отмена: проглоченная и проброшенная».
- **Примитивы asyncio не потокобезопасны.** `asyncio.Lock`, `Queue`, `Event` — только для задач одного цикла. Из другого потока в цикл передают работу через `loop.call_soon_threadsafe` или `asyncio.run_coroutine_threadsafe`.
- **Контекст запроса.** `threading.local` в asyncio не работает: все задачи в одном потоке. Для id запроса, пользователя, трассировки — `contextvars.ContextVar`: каждая задача при создании получает копию текущего контекста.

### Забытый `await`

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

### Отмена: проглоченная и проброшенная

```python
import asyncio


async def worker(swallow: bool) -> None:
    try:
        await asyncio.sleep(10)
    except BaseException as e:  # голый except ведёт себя так же
        print("поймали", type(e).__name__)
        if not swallow:
            raise
    print("корутина пошла дальше")


async def run(swallow: bool) -> None:
    t = asyncio.create_task(worker(swallow))
    await asyncio.sleep(0.1)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        print("main: await t бросил CancelledError")
    print("t.cancelled() =", t.cancelled())


for swallow in (True, False):
    print("проглочен:" if swallow else "проброшен:")
    asyncio.run(run(swallow))
```

```text title="Вывод"
проглочен:
поймали CancelledError
корутина пошла дальше
t.cancelled() = False
проброшен:
поймали CancelledError
main: await t бросил CancelledError
t.cancelled() = True
```

Если `CancelledError` проглочена, задача считает себя живой и завершается как обычная: `cancelled()` равно `False`, `await t` ничего не бросает. Для `TaskGroup` и `asyncio.timeout` это тяжело: они рассчитывают, что отмена дойдёт до границы блока.

### `ContextVar` в двух задачах

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

### `ExceptionGroup` во вложенных `TaskGroup`

Типичный код: внешний `TaskGroup` запускает функцию, у которой внутри свой `TaskGroup`. Тогда ошибка листа обёрнута дважды. Код, который берёт `eg.exceptions[0]` и ждёт там исходное исключение, работает при одной группе и ломается при двух: во внутренней группе нет атрибутов исходной ошибки (в реальном сервисе так получили `AttributeError: 'ExceptionGroup' object has no attribute 'cause'`).

```python
import asyncio


class ActivityError(Exception):
    pass


async def activity() -> None:
    raise ActivityError("нет такого тарифа")


async def fees() -> None:  # внутренняя группа
    async with asyncio.TaskGroup() as tg:
        tg.create_task(activity())


async def workflow() -> None:  # внешняя группа
    async with asyncio.TaskGroup() as tg:
        tg.create_task(fees())


def first_leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


async def main() -> None:
    try:
        await workflow()
    except ExceptionGroup as eg:
        print("exceptions[0]:", repr(eg.exceptions[0]))
        print("лист:         ", repr(first_leaf(eg)))
        print("subgroup:     ", repr(eg.subgroup(ActivityError)))

    try:
        await workflow()
    except* ActivityError as leaves:
        print("except*:      ", repr(leaves))


asyncio.run(main())
```

```text title="Вывод"
exceptions[0]: ExceptionGroup('unhandled errors in a TaskGroup', [ActivityError('нет такого тарифа')])
лист:          ActivityError('нет такого тарифа')
subgroup:      ExceptionGroup('unhandled errors in a TaskGroup', [ExceptionGroup('unhandled errors in a TaskGroup', [ActivityError('нет такого тарифа')])])
except*:       ExceptionGroup('unhandled errors in a TaskGroup', [ExceptionGroup('unhandled errors in a TaskGroup', [ActivityError('нет такого тарифа')])])
```

- `exceptions[0]` — это внутренняя группа, а не `ActivityError`.
- `subgroup(тип)` и `split(тип)` отбирают нужные листья, но сохраняют форму дерева: результат всё так же обёрнут в две группы. То же с `except*`: блок получает группу, в которой остались только подходящие исключения, вложенность не снимается.
- До самого листа дойти можно рекурсивным обходом (`first_leaf` выше) или циклом по `exceptions`, пока элемент — группа. Если нужен конкретный тип, проще отобрать его `subgroup` и обойти результат.
- Лучше, когда вложенность не важна, не лезть в `exceptions[0]` вообще, а обработать группу целиком через `except*`.

Второе правило оттуда же: done-callback не должен звать `task.result()` у отменённой задачи, там бросится `CancelledError`, и это уже ошибка в самом обработчике колбэков цикла:

```python
import asyncio


async def job(delay: float) -> int:
    await asyncio.sleep(delay)
    return 1


def bad_callback(task: asyncio.Task) -> None:
    print("результат:", task.result())


def good_callback(task: asyncio.Task) -> None:
    if task.cancelled():
        print("отменена")
    elif (exc := task.exception()) is not None:
        print("упала:", repr(exc))
    else:
        print("результат:", task.result())


async def main() -> None:
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda l, ctx: print("loop handler:", repr(ctx["exception"])))
    for cb in (bad_callback, good_callback):
        t = asyncio.create_task(job(10))
        t.add_done_callback(cb)
        await asyncio.sleep(0)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        await asyncio.sleep(0)


asyncio.run(main())
```

```text title="Вывод"
loop handler: CancelledError()
отменена
```

Первый колбэк упал на `result()` и попал в обработчик исключений цикла (без своего обработчика это просто строка в логе), второй проверяет `cancelled()` и `exception()` перед `result()`.

### Таймауты HTTP-клиента: на фазу, а не на запрос

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

### Ограничение параллелизма на 100 000 задач

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

### Блокирующий код в `async def` останавливает цикл

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

- **Почему asyncio не ускоряет вычисления?** Один поток, и пока корутина считает, она не доходит до `await` и не отдаёт управление. Вычисления выносят в процессы через `run_in_executor` или в отдельный сервис/очередь задач.
- **Потоки или asyncio для 10 000 соединений?** asyncio: задача весит килобайты, переключение — вызов функции. Потоков столько обычно не держат: память и переключения ОС. Но asyncio требует, чтобы вся цепочка была асинхронной: один синхронный драйвер БД — и выигрыш пропал.
- **Как вызвать async из sync и наоборот?** Из синхронного кода — `asyncio.run(coro())` (если цикл ещё не запущен) или `asyncio.run_coroutine_threadsafe(coro, loop).result()` из другого потока. Синхронное из асинхронного — `await asyncio.to_thread(fn)`.
- **`gather`, `wait`, `as_completed`, `TaskGroup` — в чём разница?** `gather` — результаты в порядке задач; `as_completed` — по мере готовности; `wait` — низкоуровневый, возвращает множества готовых и неготовых задач, умеет `FIRST_COMPLETED`; `TaskGroup` — структурная конкурентность: группа не завершится, пока не завершились все её задачи, и падение одной отменяет остальные.
- **Что такое uvloop?** Замена стандартного цикла событий, написанная на Cython поверх libuv (того же, что в Node.js); быстрее в 2–4 раза на сетевом вводе-выводе. Uvicorn ставит его, если он установлен.
- **Как веб-сервер на Python использует ядра?** Несколько процессов-воркеров: gunicorn с синхронными воркерами (каждый — процесс, иногда с потоками) или uvicorn/gunicorn с ASGI-воркерами (каждый — процесс с циклом событий). Обычно воркеров ставят примерно по числу ядер, в Kubernetes — чаще один процесс на под, а масштабируют подами.
- **`threading.Lock` в async-коде?** Нельзя ждать его в корутине: `acquire` заблокирует весь цикл событий. Нужен `asyncio.Lock`. И наоборот, `asyncio.Lock` не защищает от других потоков.
- **Почему `multiprocessing` на macOS падает с ошибкой pickle, а на Linux раньше работал?** На macOS по умолчанию `spawn`: функцию и аргументы нужно сериализовать, а лямбды и локальные функции pickle не умеет. Старый Linux с `fork` копировал всё как есть.
