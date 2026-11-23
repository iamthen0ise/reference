---
title: "Python: SQLAlchemy async"
---

Конспект к собеседованию по SQLAlchemy 2.x в async-коде: как устроены engine, фабрика сессий и транзакция на запрос, почему первый `SELECT` оставляет соединение в `idle in transaction`, что `rollback` делает с объектами сессии, почему ленивая загрузка в async падает и как избежать N+1, и чем `selectinload` отличается от `joinedload`. Все примеры запущены на SQLAlchemy 2.1 и asyncpg против настоящего PostgreSQL 17: поведение транзакций такое же, как в проде (на sqlite оно другое).
Рядом: [[PostgreSQL]] · [[Транзакции и уровни изоляции]] · [[Блокировки]] · [[asyncio на практике]]
Разделы: [[#Engine, сессия и транзакция на запрос]] · [[#Autobegin и idle in transaction]] · [[#Expire: commit, rollback и MissingGreenlet]] · [[#Ленивая загрузка и N+1]] · [[#selectinload и joinedload]] · [[#flush и commit]] · [[#Upsert]] · [[#Пул и pool_pre_ping]] · [[#Ловушка: classmethod без условия на объект]] · [[#Что спросят дальше]]

**Суть.** `AsyncEngine` держит пул соединений, `AsyncSession` — это единица работы (unit of work): карта загруженных объектов плюс **одна** транзакция на одном соединении из пула. Сессия на запрос, транзакция закрывается явно и до сетевых вызовов. Три правила async-режима: любой ввод-вывод — через `await` (поэтому нет неявной ленивой загрузки), связи грузятся заранее (`selectinload`, `joinedload`), после `rollback` объекты нельзя читать без `refresh`.

Модели для всех примеров (`AsyncAttrs` даёт `awaitable_attrs`):

```python
from sqlalchemy import ForeignKey, String, select, text
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(AsyncAttrs, DeclarativeBase): pass

class Author(Base):
    __tablename__ = "author"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    books: Mapped[list["Book"]] = relationship(back_populates="author")

class Book(Base):
    __tablename__ = "book"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    author_id: Mapped[int] = mapped_column(ForeignKey("author.id"))
    author: Mapped[Author] = relationship(back_populates="books")
```

В таблицах — три автора (Ann с двумя книгами, Bob и Cy по одной).

## Engine, сессия и транзакция на запрос

```python
URL = "postgresql+asyncpg://postgres:pg@localhost:55432/py_sqla"

engine = create_async_engine(URL, pool_size=5, pool_pre_ping=True)   # один на процесс
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)    # фабрика, не сессия

async def handler():
    async with SessionLocal() as session:        # сессия на запрос; закроется при выходе
        async with session.begin():              # BEGIN ... COMMIT / ROLLBACK при исключении
            session.add(Author(name="Dan"))
            session.add(Author(name="Eve"))
```

Лог SQL (event listener на `before_cursor_execute`, `begin` и `commit`), сокращённо:

```text title="Вывод"
BEGIN
INSERT INTO author (name) SELECT p0::VARCHAR FROM (VALUES ($1::VARCHAR, 0), ($2::VARCHAR, 1)) AS imp_sen(p0, sen_counter) ORDER BY sen_counter RETURNING author.id, author.id AS id__1
COMMIT
```

Два `add` ушли одним `INSERT` (insertmanyvalues), а `COMMIT` пришёл на выходе из `session.begin()`. Правила:

- **Engine один на процесс**, он владеет пулом. Фабрика `async_sessionmaker` — тоже одна; сессию создают **на запрос** (в FastAPI — зависимость с `yield`), между корутинами и задачами сессию не делят: она не потокобезопасна и не защищена от конкурентных `await` (два параллельных `await session.execute` на одной сессии дают ошибку про занятое соединение).
- `async with session.begin()` — предпочтительный способ: транзакция имеет границы в коде, на исключении откатывается сама.
- Вызвать `session.begin()` у сессии, которая уже начала транзакцию (например, после `SELECT`), нельзя: `InvalidRequestError: A transaction is already begun on this Session`. Это следующий раздел.
- `expire_on_commit=False` почти всегда нужен в async: иначе после `commit` любое чтение атрибута тянется в базу и падает (см. ниже). Но он действует **только на `commit`**.

## Autobegin и idle in transaction

Транзакцию открывать вручную не обязательно: **autobegin**. Первое же обращение к сессии (`execute`, `scalar`, `flush`) делает `BEGIN` и забирает соединение из пула. Оно остаётся занятым, пока не будет `commit` или `rollback`, даже если больше ничего не выполняется. Чтение ничего не портит, но соединение при этом висит в состоянии `idle in transaction`: держит снимок (на `READ COMMITTED` — до следующего запроса, на `REPEATABLE READ` — навсегда), мешает `VACUUM` убирать мёртвые версии строк и занимает слот пула.

```python
async def state(monitor_engine, label):
    async with monitor_engine.connect() as c:          # второе соединение
        rows = await c.execute(text(
            "select state, left(query, 40) from pg_stat_activity "
            "where datname = 'py_sqla' and pid <> pg_backend_pid() and backend_type = 'client backend'"))
        print(f"{label:<28}", [tuple(r) for r in rows])

async with SessionLocal() as s:
    await s.scalar(select(Author.id).limit(1))
    await state(mon, "после SELECT:")          # здесь в проде ходят во внешний API
    await s.rollback()
    await state(mon, "после rollback:")

async with SessionLocal() as s:
    async with s.begin():
        await s.scalar(select(Author.id).limit(1))
    await state(mon, "после выхода из begin():")
```

```text title="Вывод"
после SELECT:                [('idle in transaction', 'SELECT author.id \nFROM author \n LIMIT $1')]
после rollback:              [('idle', 'ROLLBACK;')]
после выхода из begin():     [('idle', 'COMMIT;')]
```

Это то, что видно в `pg_stat_activity`. Типичная авария: хендлер читает запись, идёт по HTTP во внешний сервис (секунды), потом пишет. Всё это время соединение `idle in transaction`; при нагрузке пул (по умолчанию `pool_size=5` + `max_overflow=10`) заканчивается, запросы встают в очередь за `pool_timeout`, а на стороне базы можно получить ещё и `idle_in_transaction_session_timeout`.

**Правило: закрыть транзакцию (`commit` или `rollback`) до сетевого вызова.** Пишущая часть — отдельная короткая транзакция после него:

```python
async with SessionLocal() as s:
    async with s.begin():                       # короткая транзакция на чтение
        order = await s.get(Order, order_id)
        payload = {"id": order.id, "total": str(order.total)}   # данные забрали в обычные типы
    status = await external_api.check(payload)  # соединение уже в пуле, транзакции нет
    async with s.begin():                       # новая транзакция на запись
        order = await s.get(Order, order_id)    # перечитать: за это время могли изменить
        order.status = status
```

Здесь `Order` и `external_api` — условные. Вторая транзакция перечитывает строку (или берёт `SELECT ... FOR UPDATE`, см. [[Блокировки]]): пока шёл вызов наружу, состояние могло поменяться. Про снимки и уровни изоляции — [[Транзакции и уровни изоляции]].

Лог `echo=True` тоже показывает границы:

```text title="Вывод"
BEGIN (implicit)
SELECT author.id FROM author LIMIT $1::INTEGER
[generated in 0.00005s] (1,)
-- внешний вызов --
COMMIT
```

`BEGIN (implicit)` — это и есть autobegin: в коде `BEGIN` нет, а в логе он есть.

## Expire: commit, rollback и MissingGreenlet

Сессия помнит загруженные объекты и значения их атрибутов. **Expire** помечает значения устаревшими: следующее чтение атрибута отправит `SELECT` и перечитает строку. По умолчанию это происходит на каждый `commit` (`expire_on_commit=True`) и на каждый `rollback`. Чтение устаревшего атрибута — неявный ввод-вывод, а в async его делать нельзя.

`expire_on_commit=False` отключает expire **только на `commit`**. На `rollback` объекты по-прежнему становятся устаревшими:

```python
async with SessionLocal() as s:                  # expire_on_commit=False
    a = await s.scalar(select(Author).where(Author.name == "Ann"))
    await s.commit()
    print("после commit:  ", a.name, "| expired:", inspect(a).expired_attributes)

    a = await s.scalar(select(Author).where(Author.name == "Ann"))
    await s.rollback()
    print("после rollback expired:", inspect(a).expired_attributes)
    print(a.name)
```

```text title="Вывод"
после commit:   Ann | expired: set()
после rollback expired: {'id', 'name', 'books'}
MissingGreenlet - greenlet_spawn has not been called; can't call await_() here. Was IO attempted in an unexpected place?
```

Объект после `commit` читается, после `rollback` — нет: атрибут устарел, SQLAlchemy пытается перечитать его синхронно из `__get__`, а в async-контексте синхронный ввод-вывод запрещён, отсюда `MissingGreenlet` (`sqlalchemy.exc.MissingGreenlet`). Ошибка вылетает не на `rollback`, а позже, на каком-нибудь `order.status` при сборке ответа, поэтому её трудно связать с причиной. Логика ORM: данные, прочитанные в откаченной транзакции, нельзя считать актуальными, и их перечитывают заново.

Как делать правильно:

```python
async with SessionLocal() as s:
    a = await s.scalar(select(Author).where(Author.name == "Bob"))
    dto = {"id": a.id, "name": a.name}      # 1) собрать данные ДО rollback
    await s.rollback()
    print("DTO:", dto)
    await s.refresh(a)                       # 2) или явно перечитать после
    print("refresh:", a.name)
```

```text title="Вывод"
DTO: {'id': 2, 'name': 'Bob'}
refresh: Bob
```

Варианты по убыванию надёжности:

1. **DTO до `rollback`** (Pydantic-модель или словарь). Ответ не зависит от состояния сессии вообще.
2. `await session.refresh(obj)` — один лишний `SELECT`, зато объект снова живой. `refresh(obj, ["books"])` обновляет только указанные атрибуты.
3. Не откатывать посреди логики: если `rollback` нужен лишь затем, чтобы закрыть транзакцию перед сетевым вызовом, вызывать `commit()` (при `expire_on_commit=False` объекты остаются читаемыми) либо собирать нужные значения заранее, как в примере из предыдущего раздела.

## Ленивая загрузка и N+1

Связь по умолчанию грузится лениво (`lazy="select"`): при первом обращении к `author.books` уходит отдельный запрос. В async это обращение — синхронный ввод-вывод, и оно падает:

```python
async with SessionLocal() as s:
    authors = (await s.scalars(select(Author).order_by(Author.id))).all()
    authors[0].books
```

```text title="Вывод"
MissingGreenlet: greenlet_spawn has not been called; can't call await_() here. Was IO attempted in an unexpected place?
```

Тот же `MissingGreenlet`, что при expire. Чтобы всё же загрузить лениво, используют `AsyncAttrs` и `awaitable_attrs`. Это работает, но делает **N+1**: один запрос за авторами и по одному за книгами каждого.

```python
async with SessionLocal() as s:
    authors = (await s.scalars(select(Author).order_by(Author.id))).all()
    for a in authors:
        await a.awaitable_attrs.books
```

```text title="Вывод"
--- N+1: 4 запр.
   SELECT author.id, author.name FROM author ORDER BY author.id
   SELECT book.id, book.title, book.author_id FROM book WHERE $1::INTEGER = book.author_id
   SELECT book.id, book.title, book.author_id FROM book WHERE $1::INTEGER = book.author_id
   SELECT book.id, book.title, book.author_id FROM book WHERE $1::INTEGER = book.author_id
```

Три автора дали 1 + 3 запроса; сто авторов дали бы 101. Лечится жадной загрузкой в самом запросе: `options(selectinload(...))` или `joinedload(...)`. Чтобы в async вообще нельзя было случайно сходить в ленивую загрузку, ставят `lazy="raise"` на связи: любое обращение без явной загрузки падает сразу с понятной ошибкой, и N+1 не пролезает незамеченным.

## selectinload и joinedload

`selectinload` — **второй запрос** с `IN (...)` по ключам родителей:

```python
authors = (await s.scalars(select(Author).options(selectinload(Author.books)))).all()
```

```text title="Вывод"
--- selectinload: 2 запр.
   SELECT author.id, author.name FROM author
   SELECT book.author_id, book.id, book.title FROM book WHERE book.author_id IN ($1::INTEGER, $2::INTEGER, $3::INTEGER)
```

`joinedload` — **один запрос** с `LEFT OUTER JOIN`. Для коллекции строка автора повторяется для каждой книги, и результат нужно «схлопнуть» через `unique()`; без него SQLAlchemy падает:

```python
r = await s.scalars(select(Author).options(joinedload(Author.books)))
r.all()
```

```text title="Вывод"
InvalidRequestError: The unique() method must be invoked on this Result, as it contains results that include joined eager loads against collections
```

```python
authors = r.unique().all()          # 3 автора, а не 4 строки
```

```text title="Вывод"
--- joinedload + unique(): 1 запр.
   SELECT author.id, author.name, book_1.id AS id_1, book_1.title, book_1.author_id FROM author LEFT OUTER JOIN book AS book_1 ON author.id = book_1.author_id
```

Для связи «много к одному» (книга → автор) `unique()` не нужен, и один `JOIN` — лучший выбор:

```text title="Вывод"
--- joinedload many-to-one: 1 запр.
   SELECT book.id, book.title, book.author_id, author_1.id AS id_1, author_1.name FROM book LEFT OUTER JOIN author AS author_1 ON author_1.id = book.author_id
```

| | `selectinload` | `joinedload` |
| --- | --- | --- |
| Запросов | 2 (или 1 + число пачек по ключам) | 1 |
| Коллекция «один ко многим» | подходит: строки родителя не дублируются | дублирует родителя в каждой строке, нужен `unique()` |
| «Много к одному» | избыточно | подходит |
| `LIMIT` на родителях | работает как задумано | `LIMIT` ограничивает строки JOIN, а не родителей: SQLAlchemy оборачивает запрос в подзапрос |
| Широкие родительские строки | не повторяются | повторяются столько раз, сколько детей |

Практика: коллекции — `selectinload`, скалярные связи — `joinedload`; в async на все связи, которые читаются в ответе, ставят явную жадную загрузку.

## flush и commit

`flush` отправляет накопленные изменения в базу (`INSERT`/`UPDATE`/`DELETE`), но транзакция остаётся открытой: другие соединения изменений не видят. `commit` фиксирует транзакцию.

```python
async with SessionLocal() as s:
    s.add(Author(name="Fay"))
    await s.flush()
    print("видит другое соединение:", await count_fay(other))   # 0
    await s.commit()
    print("видит другое соединение:", await count_fay(other))   # 1
```

```text title="Вывод"
после flush, id: 6 | видит другое соединение: 0
после commit, видит другое соединение: 1
```

`flush` нужен, чтобы получить `id`, сгенерированный базой, или проверить ограничение (`IntegrityError` вылетает на `flush`, а не на `add`). Сессия сама вызывает `flush` перед каждым `execute`/`scalars` (autoflush), поэтому запрос видит свои же ещё не закоммиченные изменения. `id` из примера недетерминирован (зависит от последовательности).

## Upsert

`INSERT ... ON CONFLICT` в SQLAlchemy — диалектная вставка из `sqlalchemy.dialects.postgresql`:

```python
from sqlalchemy.dialects.postgresql import insert

async with SessionLocal() as s, s.begin():
    for n in ("Ann", "Zed"):            # Ann уже есть, Zed новая
        stmt = insert(Author).values(name=n)
        stmt = stmt.on_conflict_do_update(index_elements=[Author.name], set_={"name": stmt.excluded.name})
        await s.execute(stmt)
    names = (await s.execute(select(Author.name).order_by(Author.id))).scalars().all()
    print(names)
```

```text title="Вывод"
['Ann', 'Bob', 'Cy', 'Dan', 'Eve', 'Fay', 'Zed']
INSERT INTO author (name) VALUES ($1::VARCHAR) ON CONFLICT (name) DO UPDATE SET name = excluded.name RETURNING author.id
```

Ann не продублировалась: на конфликте по уникальному `name` сработал `DO UPDATE`. `stmt.excluded` — строка, которую пытались вставить. `on_conflict_do_nothing` молча пропускает конфликт, и тогда `RETURNING` ничего не вернёт:

```python
stmt = insert(Author).values(name="Ann").on_conflict_do_nothing(index_elements=[Author.name]).returning(Author.id)
print((await s.execute(stmt)).scalar_one_or_none())
```

```text title="Вывод"
None
```

Атомарность даёт сама база: «сначала `SELECT`, потом `INSERT`» под нагрузкой гоняется, а `ON CONFLICT` — одна операция с блокировкой строки. Цель конфликта (`index_elements` или `constraint=`) должна быть уникальным индексом. Подробнее про механику — [[Блокировки]] и [[PostgreSQL]].

## Пул и pool_pre_ping

Пул держит открытые соединения и выдаёт их сессиям. Соединение в пуле может умереть: перезапуск базы, `pg_terminate_backend`, обрыв на балансировщике. Обычный пул об этом не знает и выдаёт мёртвое соединение. `pool_pre_ping=True` перед выдачей отправляет лёгкий пинг и, если соединение мертво, открывает новое:

```python
engine = create_async_engine(URL, pool_size=1, pool_pre_ping=pre)
async with engine.connect() as c:
    pid = await c.scalar(text("select pg_backend_pid()"))     # соединение вернулось в пул
async with admin_engine.connect() as k:                        # убиваем его со стороны сервера
    await k.execute(text("select pg_terminate_backend(:p)"), {"p": pid})
async with engine.connect() as c:                              # пул выдаёт то же соединение
    print(f"pre_ping={pre}:", await c.scalar(text("select 1")))
```

```text title="Вывод"
pre_ping=False: InternalError - (sqlalchemy.dialects.postgresql.asyncpg.InternalClientError) cannot switch to state 15; another operation (2) is in progress
pre_ping=True: 1
```

Без пинга первый запрос после обрыва падает (текст ошибки зависит от драйвера и момента обрыва), с пингом — проходит. Цена — один дополнительный круг до базы на выдачу соединения. Дополнительно: `pool_recycle` пересоздаёт соединения старше N секунд (для прокси, рвущих долгие соединения); `pool_size` + `max_overflow` — верхняя граница соединений процесса, и суммарно по всем процессам она не должна превышать `max_connections` базы.

## Ловушка: classmethod без условия на объект

Метод, который нужен про **конкретный** объект, написан как `@classmethod` и фильтрует по `cls.<столбец>`. Запрос уходит без условия на экземпляр и молча идёт по всей таблице:

```python
class Doc(Base):
    __tablename__ = "doc"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int]
    name: Mapped[str]

    @classmethod
    async def named(cls, session, name):            # БАГ: нет условия на конкретный объект
        return (await session.scalars(select(cls).where(cls.name == name))).all()

    async def my_named(self, session, name):        # условие на владельца
        q = select(Doc).where(Doc.owner_id == self.owner_id, Doc.name == name)
        return (await session.scalars(q)).all()
```

Тест с одним объектом зелёный, с двумя — красный (число найденных строк у «баговой» и правильной версии):

```text title="Вывод"
один объект:   1 1
два объекта:   2 1
```

Вторая строка — реальная утечка данных между владельцами: пользователь запрашивает свой документ и получает чужой. Правило для тестов на выборки: **как минимум два объекта с одинаковыми значениями фильтруемого поля у разных владельцев**. Тест с одним объектом проверяет только, что запрос что-то возвращает.

## Что спросят дальше

- **Чем сессия отличается от соединения?** Соединение — сокет к базе из пула. Сессия — единица работы ORM: карта идентичности (identity map) объектов и текущая транзакция; соединение она берёт из пула на время транзакции (при autobegin — с первого запроса) и возвращает на `commit`/`rollback`/`close`. Сессий может быть много на одном процессе, соединений — не больше размера пула.
- **`flush` против `commit`?** `flush` пишет изменения в рамках открытой транзакции (другие соединения их не видят, откат возможен), `commit` фиксирует. Autoflush вызывает `flush` перед запросами автоматически. Как работает видимость между транзакциями — [[Транзакции и уровни изоляции]].
- **`scalars().all()` или `execute().all()`?** `execute()` возвращает строки `Row` (даже для одного столбца — кортежи), `scalars()` берёт первый столбец каждой строки, то есть объекты ORM при `select(Author)`. Для `joinedload` коллекций дальше нужен `unique()`. Если нужна ровно одна строка: `scalar_one()` (ошибка при нуле или нескольких), `scalar_one_or_none()`, `first()` (первую, остальные отбрасывает).
- **Как сделать upsert?** `postgresql.insert(...).on_conflict_do_update(index_elements=..., set_=...)`; `on_conflict_do_nothing` + `returning` для «создать, если нет». Один атомарный запрос, а не `SELECT` + `INSERT`.
- **Зачем `pool_pre_ping` и какие ещё настройки пула важны?** Проверка соединения перед выдачей (мёртвые из-за рестарта или таймаута прокси). Плюс `pool_size`/`max_overflow`/`pool_timeout`, `pool_recycle`; сумма по процессам не больше `max_connections`.
- **Почему `MissingGreenlet`?** Атрибут устарел (после `rollback`, после `commit` при `expire_on_commit=True`) или связь не загружена, и ORM пытается синхронно сходить в базу в async-контексте. Лечение: жадная загрузка (`selectinload`/`joinedload`), `lazy="raise"` для раннего падения, DTO до `rollback`, `refresh`.
- **Почему нельзя держать транзакцию во время внешнего вызова?** Автобегин оставляет соединение `idle in transaction`: оно занимает слот пула, держит снимок и блокирует очистку мёртвых версий. Транзакцию закрывают до сетевого вызова. Подробно — [[Блокировки]] и [[PostgreSQL]].
