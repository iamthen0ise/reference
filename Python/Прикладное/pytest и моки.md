---
title: "Python: pytest и моки"
---

Конспект к собеседованию по тестам в Python: фикстуры pytest, `parametrize`, `monkeypatch`; что и где патчить в `unittest.mock`, как `caplog` пропускает записи и почему тест может проходить на пустоте.
Рядом: [[Логирование]] (иерархия логгеров, на которой держится `caplog`) · [[Декораторы]] и [[Контекстные менеджеры]] (на них держатся `patch` и фикстуры) · [[asyncio на практике]] (`AsyncMock` и корутины).
Разделы: [[#pytest: фикстуры и параметризация]] · [[#Что и где патчить]] · [[#caplog и логгеры мимо него]] · [[#Тест, который ничего не ловит]] · [[#Что спросят дальше]]

**Суть.** Тест проверяет только то, что мы сумели увидеть: если запись ушла мимо перехватчика, а мок подменил не то имя, тест зелёный и бесполезный. Поэтому после написания теста его ломают: убирают проверяемое поведение и смотрят, что тест упал.

## pytest: фикстуры и параметризация

Фикстура — функция с `@pytest.fixture`; тест просит её по имени аргумента. **scope** определяет, как долго живёт значение: `function` (по умолчанию, каждый тест заново), `class`, `module`, `package`, `session`. Чем шире scope, тем меньше затрат на создание и тем больше риска, что тесты начнут влиять друг на друга через общее состояние.

Фикстура с `yield` — это setup, затем тест, затем teardown (код после `yield`), даже если тест упал:

```python
# conftest.py
import pytest

@pytest.fixture(scope="session")
def cfg():
    return {"dsn": "x"}

@pytest.fixture
def conn(cfg):                 # фикстура зависит от другой фикстуры
    c = {"open": True}
    yield c
    c["open"] = False          # очистка после теста

# test_a.py
import os, pytest

def test_one(conn):
    assert conn["open"]

@pytest.mark.parametrize("a,b,exp", [(1, 2, 3), (0, 0, 0), (-1, 1, 0), (2, 2, 5)])
def test_add(a, b, exp):
    assert a + b == exp

def test_tmp(tmp_path):                 # свежий каталог на каждый тест
    (tmp_path / "f.txt").write_text("hi")

def test_env(monkeypatch):
    monkeypatch.setenv("MODE", "test")
    assert os.environ["MODE"] == "test"

def test_env_gone():
    assert "MODE" not in os.environ
```

```text title="Вывод"
$ pytest -q
....F...                                                                 [100%]
>       assert a + b == exp
E       assert (2 + 2) == 5
FAILED test_a.py::test_add[2-2-5] - assert (2 + 2) == 5
1 failed, 7 passed in 0.10s
```

Восемь тестов из пяти функций: `parametrize` развернул `test_add` в четыре независимых случая (`test_add[2-2-5]`), упал только один. `tmp_path` — свежий `pathlib.Path`; `test_env_gone` проходит, потому что `monkeypatch` откатывает правку окружения. `monkeypatch` умеет `setattr`, `setenv`, `setitem`, `chdir`, `delattr`. Teardown фикстур идёт в обратном порядке setup и выполняется, даже если тест упал (`pytest --setup-show` показывает порядок).

## Что и где патчить

`unittest.mock.patch("a.b")` находит объект по строке `"a.b"` и подменяет **атрибут** `b` в модуле `a` на время `with`. Правило: патчить имя там, где его **ищут** (в модуле, который тестируем), а не там, где оно определено.

### from x import y против import x

```python
# mod3.py
from time import sleep           # имя sleep скопировано в mod3 при импорте
def wait(): sleep(0.3)
```

```python
import time, mod3
from unittest.mock import patch

with patch("time.sleep"):        # подменяем в модуле time, но mod3.sleep уже ссылается на оригинал
    t = time.perf_counter(); mod3.wait()
    print("time.sleep patched, waited %.1f s" % (time.perf_counter() - t))

with patch("mod3.sleep"):        # подменяем имя в том модуле, который его ищет
    t = time.perf_counter(); mod3.wait()
    print("mod3.sleep patched, waited %.1f s" % (time.perf_counter() - t))
```

```text title="Вывод"
time.sleep patched, waited 0.3 s
mod3.sleep patched, waited 0.0 s
```

Первый патч не сработал: `from time import sleep` привязал имя `mod3.sleep` к функции в момент импорта, подмена атрибута `time.sleep` этой ссылки не касается. Если бы в модуле был `import time` и вызов `time.sleep(...)`, сработал бы и `patch("time.sleep")` — но, как показывает следующий пример, ценой общего объекта.

### patch("mymodule.asyncio.sleep") подменяет asyncio.sleep для всех

`mymodule.asyncio` — это не копия, а тот самый объект-модуль `asyncio`, один на весь процесс. Поэтому `patch("mymodule.asyncio.sleep")` на самом деле делает `setattr(asyncio, "sleep", mock)`:

```python
# mymodule.py
import asyncio
async def retry():
    await asyncio.sleep(1)
```

```python
import asyncio, mymodule
from unittest.mock import patch

async def main():
    with patch("mymodule.asyncio.sleep") as s:
        print(asyncio.sleep is s, mymodule.asyncio is asyncio)
asyncio.run(main())
print(asyncio.sleep)
```

```text title="Вывод"
True True
<function sleep at 0x...>
```

Внутри `with` `asyncio.sleep is s` истинно: подменён общий объект. Все, кто в это время зовёт `asyncio.sleep`, — pytest-asyncio, соседние задачи, сторонние библиотеки — получат мок, и «таймауты» вдруг перестанут ждать. Вне `with` оригинал возвращается.

Безопаснее подменить в тестируемом модуле само имя: `monkeypatch.setattr(mymodule, "asyncio", types.SimpleNamespace(sleep=fake_sleep))` (`fake_sleep` — `async def`, пишущий аргумент в список) — подмена видна только `mymodule` и откатывается сама.

`monkeypatch.setattr(obj, "name", value)` на конкретном объекте (экземпляре, классе, модуле) надёжнее строки в `patch`: опечатка в имени атрибута даёт `AttributeError` сразу (при `raising=True`, по умолчанию), и откат гарантирован фикстурой.

### autospec и AsyncMock

`MagicMock()` принимает любое обращение, в том числе опечатку в имени метода: тест «проверил», что `repo.gett` вызван, а настоящего метода `get` никто не вызывал. `create_autospec` (или `patch(..., autospec=True)`) копирует сигнатуры настоящего объекта и ловит это:

```python
import asyncio
from unittest.mock import MagicMock, create_autospec, AsyncMock

class Repo:
    def get(self, id): ...
    async def save(self, obj): ...

m = MagicMock()
m.gett(1)
print("MagicMock typo ok:", m.gett.called)

s = create_autospec(Repo, instance=True)
try: s.gett(1)
except AttributeError as e: print("autospec:", e)
try: s.get(1, 2)
except TypeError as e: print("autospec:", e)

async def main():
    am = AsyncMock(return_value=7)
    print(await am(), am.await_count)
asyncio.run(main())
```

```text title="Вывод"
MagicMock typo ok: True
autospec: Mock object has no attribute 'gett'
autospec: too many positional arguments
7 1
```

`AsyncMock` — мок корутинной функции: вызов возвращает корутину, `await` даёт `return_value`, а `await_count` и `assert_awaited_once_with` проверяют именно ожидание (вызвать, но не `await` — тоже баг). `patch` сам выбирает `AsyncMock`, если цель — `async def`. `await MagicMock()()` даёт `TypeError: 'MagicMock' object can't be awaited`. Про корутины и цикл событий — [[asyncio на практике]].

## caplog и логгеры мимо него

`caplog` — фикстура pytest, которая вешает свой хендлер на логгеры и собирает записи: `caplog.records` (объекты `LogRecord`), `caplog.text` (отформатированный текст), `caplog.messages`. Уровень поднимают на время блока: `with caplog.at_level(logging.INFO, logger="svc"):` — иначе записи ниже WARNING (уровень root по умолчанию) не создаются.

```python
with caplog.at_level(logging.INFO, logger="svc2"):
    svc.work("A1")
assert [r.getMessage() for r in caplog.records] == ["retry A1"]
```

### Ловушка: логгер с propagate=False и своим хендлером

Хендлер `caplog` висит на root и (в новых pytest) на тех логгерах с `propagate=False`, которые уже существуют в момент входа в фикстуру. Не видит он:

- логгеры, созданные напрямую (`logging.Logger("svc")`, `StructuredLogger(...)` без `getLogger` — их нет в реестре логгеров);
- логгеры, которым `propagate = False` поставили уже во время теста;
- запись, ушедшую вообще не через `logging`, например `print(..., file=sys.stderr)`.

Если такой логгер пишет в stderr своим хендлером, тест «в логах нет секрета/ошибок» через `caplog.records` проходит на пустоте — записей он не видел. Хуже того, `capsys` тоже может не помочь: `StreamHandler(sys.stderr)` запоминает объект потока при импорте, а `capsys` подменяет `sys.stderr` позже.

```python
# applog.py
import logging, sys

log = logging.Logger("svc")                 # создан напрямую, мимо реестра
log.addHandler(logging.StreamHandler(sys.stderr))   # stderr запомнен при импорте

def charge(code):
    log.error("bad code %s", code)
```

```python
# test_l.py
import logging
import applog

def test_caplog_vacuous(caplog):
    applog.charge("SECRET")
    assert not caplog.records                       # проходит, хотя ERROR был

def test_capsys_vacuous(capsys):
    applog.charge("SECRET")
    assert "SECRET" not in capsys.readouterr().err  # тоже проходит

def test_own_handler():
    seen = []
    class H(logging.Handler):
        def emit(self, r): seen.append(r.getMessage())
    h = H(); applog.log.addHandler(h)
    try:
        applog.charge("SECRET")
    finally:
        applog.log.removeHandler(h)
    assert "SECRET" not in " ".join(seen)           # падает: запись видна
```

```text title="Вывод"
$ pytest -q
bad code SECRET
FAILED test_l.py::test_own_handler - AssertionError: assert 'SECRET' not in '...
1 failed, 2 passed in 0.01s
```

Строка `bad code SECRET` выскочила в настоящий stderr мимо обоих перехватчиков, два «защитных» теста зелёные, а правильный — красный: он честно видит запись. Как проверять, а не гадать: добавить на логгер собственный `logging.Handler` со списком записей (как выше) и снять его в `finally`; либо подменить логгер в модуле (`patch.object(applog, "log")`) и проверять `lg.error.call_args`. Рядом нужен парный тест на «есть»: проверка «нет» без него ничего не доказывает.

## Тест, который ничего не ловит

Зелёный тест не доказывает, что код верен: он доказывает, что при этих входных данных ничего не упало. Проверка теста — сломать код и убедиться, что тест стал красным (мутационное тестирование в малом). Типичный случай — данные, которые не отличают правильный код от неправильного.

```python
# repo.py
def get_by_name(rows, owner, name):
    # баг: owner не учитывается — выборка по всей таблице
    return next((r for r in rows if r["name"] == name), None)
```

```python
# test_r.py
import repo
ONE = [{"owner": 1, "name": "thumb", "id": 10}]
TWO = ONE + [{"owner": 2, "name": "thumb", "id": 20}]

def test_one_object():
    assert repo.get_by_name(ONE, 1, "thumb")["id"] == 10

def test_two_objects():
    assert repo.get_by_name(TWO, 2, "thumb")["id"] == 20
```

```text title="Вывод"
$ pytest -q test_r.py
    def test_two_objects():
>       assert repo.get_by_name(TWO, 2, "thumb")["id"] == 20
E       assert 10 == 20

FAILED test_r.py::test_two_objects - assert 10 == 20
1 failed, 1 passed in 0.01s
```

С одним объектом «выборка по всей таблице» неотличима от выборки «по владельцу»: единственная строка и так единственная. Нужен хотя бы второй объект, который **должен быть отфильтрован**. Приёмы: перед доверием к тесту временно сломать код и увидеть красный результат; в данных держать «соседа», которого код обязан не тронуть (чужой владелец, удалённая запись, другой tenant); в моках — `assert_called_once_with(...)` с конкретными аргументами, а не `called`; `for r in records: assert ...` на пустом списке проходит вхолостую — сначала `assert records`; у проверки «нет в логах» нужна парная «есть».

## Что спросят дальше

- Как работает область видимости фикстур и какой scope выбрать для подключения к базе, для HTTP-клиента, для временных файлов? Что произойдёт с teardown, если упал setup?
- Что именно подменяет `patch("pkg.mod.asyncio.sleep")` и как сделать это безопасно? Где патчить при `from x import y` и при `import x`?
- Как убедиться, что тест на «в логах нет ошибки» не проходит на пустоте? Как проверить, что тест вообще способен упасть?
