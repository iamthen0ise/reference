---
title: "Python: MRO и super()"
---

В каком порядке Python ищет методы при множественном наследовании (MRO и C3-линеаризация) и как `super()` идёт не к «родителю», а к следующему классу в этой цепочке.
Рядом: [[Создание объектов]] (`__new__`, `__init__`, метаклассы) · [[Атрибуты и дескрипторы]] (поиск атрибута, `__slots__`).
Разделы: [[#Порядок разрешения методов (MRO)]] · [[#super() и кооперативное наследование]] · [[#Что спросят дальше]]

**Суть.** Методы и атрибуты ищутся по цепочке классов в порядке MRO (method resolution order, порядок разрешения методов), который Python вычисляет алгоритмом C3. `super()` идёт не к «родителю», а к следующему классу в этой цепочке.

## Порядок разрешения методов (MRO)

**MRO** — список классов, в котором Python ищет атрибут или метод: сначала сам класс, потом его родители в определённом порядке, в конце `object`. Посмотреть: `C.__mro__` или `C.mro()`. При одиночном наследовании всё очевидно: потомок, родитель, дед. Интересно становится при множественном наследовании, особенно в «ромбе» (diamond):

```python
class A:
    def hello(self) -> str:
        return "A"


class B(A):
    pass


class C(A):
    def hello(self) -> str:
        return "C"


class D(B, C):
    pass


print([k.__name__ for k in D.__mro__])
print(D().hello())
```

```text title="Вывод"
['D', 'B', 'C', 'A', 'object']
C
```

Если бы поиск шёл простым обходом в глубину слева направо, порядок был бы `D, B, A, C, A`: метод нашёлся бы в `A` раньше, чем в `C`, и переопределение в `C` никогда бы не сработало. Так было у «классических» классов в Python 2. С Python 2.3 MRO вычисляется **C3-линеаризацией**, которая гарантирует три свойства:

1. **Потомок раньше родителей**: класс всегда стоит перед своими базовыми классами.
2. **Порядок родителей сохраняется**: в `class D(B, C)` класс `B` всегда раньше `C`.
3. **Монотонность**: если в MRO родителя `X` стоит раньше `Y`, то и в MRO любого потомка `X` стоит раньше `Y`. Порядок, который был у родителя, потомок не ломает.

### Как считать C3 вручную

Формула: MRO класса = сам класс + слияние (merge) MRO всех его родителей и списка самих родителей в порядке объявления.

```text
L[C] = C + merge(L[P1], L[P2], ..., L[Pn], [P1, P2, ..., Pn])
```

Слияние: смотрим на первые элементы (головы) списков слева направо и берём первую голову, которая **не встречается в хвосте** (во всех позициях, кроме первой) ни одного из списков. Берём её в результат, вычёркиваем из всех списков и повторяем. Если ни одна голова не подходит — непротиворечивого порядка не существует, Python бросает `TypeError`.

Ромб выше, `O` — это `object`:

```text
L[A] = [A, O]
L[B] = [B, A, O]
L[C] = [C, A, O]
L[D] = D + merge([B, A, O], [C, A, O], [B, C])

1. Голова B: в хвостах [A, O], [A, O], [C] её нет → берём B.
   Остались [A, O], [C, A, O], [C]
2. Голова A: есть в хвосте [C, A, O] → пропускаем.
   Голова C: в хвостах нет → берём C.
   Остались [A, O], [A, O], []
3. Берём A, затем O.

L[D] = [D, B, C, A, O]
```

Пример, где порядок невозможен:

```python
class A: pass
class B(A): pass

try:
    class C(A, B): pass  # A объявлен раньше B, но B — потомок A и должен быть раньше
except TypeError as e:
    print(e)
```

```text title="Вывод"
Cannot create a consistent method resolution order (MRO) for bases A, B
```

`merge([A, O], [B, A, O], [A, B])`: голова `A` есть в хвосте `[B, A, O]`, голова `B` — в хвосте `[A, B]`, третий список снова начинается с `A`. Подходящей головы нет. Та же ошибка будет, если `X(A, B)` и `Y(B, A)` наследуются вместе: `class Z(X, Y)`.

### C3 своими руками

Иногда просят написать алгоритм. Он короткий:

```python
def c3_mro(cls: type) -> list[type]:
    bases = list(cls.__bases__)
    return [cls] + c3_merge([c3_mro(base) for base in bases] + [bases])


def c3_merge(seqs: list[list[type]]) -> list[type]:
    seqs = [list(s) for s in seqs if s]
    result = []
    while seqs:
        for seq in seqs:
            head = seq[0]
            if not any(head in other[1:] for other in seqs):
                break
        else:
            raise TypeError("Cannot create a consistent MRO")
        result.append(head)
        # подходящая голова не встречается в хвостах, значит, она может быть только в начале списков
        seqs = [s[1:] if s[0] is head else s for s in seqs]
        seqs = [s for s in seqs if s]
    return result


class O: pass
class A(O): pass
class B(O): pass
class C(O): pass
class D(O): pass
class E(O): pass
class K1(A, B, C): pass
class K2(D, B, E): pass
class K3(D, A): pass
class Z(K1, K2, K3): pass

print([k.__name__ for k in c3_mro(Z)])
assert c3_mro(Z) == list(Z.__mro__)
```

```text title="Вывод"
['Z', 'K1', 'K2', 'K3', 'D', 'A', 'B', 'C', 'E', 'O']
```

Сложность — O(n²) и выше по числу классов, но иерархии маленькие, а MRO вычисляется один раз при создании класса и хранится в `__mro__`.

## super() и кооперативное наследование

`super()` возвращает объект-посредник, который ищет атрибут в MRO **типа объекта `self`**, начиная со следующего класса после текущего. Не «у родителя», а «у следующего в цепочке». В ромбе `super()` внутри `B` ведёт в `C`, хотя `B` про `C` ничего не знает:

```python
class A:
    def hello(self) -> list[str]:
        return ["A"]


class B(A):
    def hello(self) -> list[str]:
        return ["B", *super().hello()]


class C(A):
    def hello(self) -> list[str]:
        return ["C", *super().hello()]


class D(B, C):
    def hello(self) -> list[str]:
        return ["D", *super().hello()]


print(D().hello())  # A ровно один раз
print(B().hello())  # у объекта B другой MRO, и super() в B ведёт в A
```

```text title="Вывод"
['D', 'B', 'C', 'A']
['B', 'A']
```

Благодаря этому каждый метод выполняется ровно один раз, даже если до класса можно дойти несколькими путями. Если бы `B` вызывал `A.hello(self)` напрямую, `C.hello` был бы пропущен, а в более сложной иерархии `A` вызвался бы дважды. Порядок вызовов с печатью (`D(B, C)`, у каждого `hello` вызывает `super().hello()` между двумя `print`):

```python
class A:
    def hello(self):
        print("A.hello")


class B(A):
    def hello(self):
        print("B.hello: до")
        super().hello()
        print("B.hello: после")


class C(A):
    def hello(self):
        print("C.hello: до")
        super().hello()
        print("C.hello: после")


class D(B, C):
    def hello(self):
        print("D.hello: до")
        super().hello()
        print("D.hello: после")


D().hello()
```

```text title="Вывод"
D.hello: до
B.hello: до
C.hello: до
A.hello
C.hello: после
B.hello: после
D.hello: после
```

Вызовы идут вперёд по MRO (`D, B, C, A`) и возвращаются назад. Если `B` вызывает `A.hello(self)` напрямую, `C.hello` не выполнится совсем:

```python
class B2(A):
    def hello(self):
        print("B2.hello")
        A.hello(self)  # в обход MRO


class D2(B2, C):
    pass


D2().hello()
```

```text title="Вывод"
B2.hello
A.hello
```

Чтобы цепочка работала, классы должны **кооперироваться**:

- каждый класс в иерархии вызывает `super().method(...)`; один забыл — все классы после него в MRO не вызовутся;
- сигнатуры совместимы. Для `__init__` это значит: каждый забирает свои именованные аргументы и передаёт остальное дальше через `**kwargs`, а до `object.__init__` должно дойти пусто.

```python
class Storage:
    def __init__(self, *, dsn: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.dsn = dsn


class LoggingMixin:
    def __init__(self, *, logger_name: str = "app", **kwargs) -> None:
        super().__init__(**kwargs)
        self.logger_name = logger_name


class CacheMixin:
    def __init__(self, *, ttl: int = 60, **kwargs) -> None:
        super().__init__(**kwargs)
        self.ttl = ttl


class UserRepo(LoggingMixin, CacheMixin, Storage):
    pass


repo = UserRepo(dsn="postgresql://localhost/app", ttl=30)
print([k.__name__ for k in UserRepo.__mro__])
print(repo.dsn, repo.ttl, repo.logger_name)
```

```text title="Вывод"
['UserRepo', 'LoggingMixin', 'CacheMixin', 'Storage', 'object']
postgresql://localhost/app 30 app
```

Опечатка в имени аргумента проходит через все `**kwargs` и падает только в самом конце цепочки, в `object.__init__` (здесь в каждом `__init__` добавлена печать имени класса перед `super().__init__`, `UserRepo(dsn="x", typo=1)`):

```text title="Вывод"
LoggingMixin
CacheMixin
Storage
TypeError: object.__init__() takes exactly one argument (the instance to initialize)
```

**Миксин** (mixin, «примесь») — маленький класс, который добавляет одну возможность и сам по себе не используется. Миксины ставят **левее** основного класса: тогда их методы оказываются раньше в MRO и могут обернуть метод основного класса через `super()`. Так устроены class-based views в Django (`LoginRequiredMixin, ListView`).

`super()` без аргументов работает благодаря скрытой ячейке `__class__`, которую компилятор создаёт для каждого метода, где упомянуты `super` или `__class__`. Поэтому он не работает в функции, которую объявили вне класса и потом присвоили атрибуту класса. Там пишут `super(Класс, self)` явно.

## Что спросят дальше

- **Почему `super().__init__()` нужно вызывать даже в миксине без родителей?** Родитель миксина — `object`, но в MRO итогового класса после миксина могут стоять другие классы. Не вызвал `super()` — их `__init__` не выполнится.
