---
title: Память в Linux
---

Что процесс считает своей памятью и сколько он занимает на самом деле: чем VSZ отличается от RSS, почему «Linux съел всю память» и что про это говорит `free`, откуда у `free` в контейнере чужие цифры, почему `malloc` гигабайта не падает и как OOM killer выбирает жертву. Всё показано прогоном в песочнице (Debian в Docker с привилегиями), а не пересказом man.

Как читать. Поднять песочницу: `make linux-up`; блок запускается `make linux-block PAGE="Linux/Память в Linux.md" TITLE="<имя>"`, вывод ниже снят именно так. Ядро в песочнице — ядро виртуальной машины Docker Desktop (linuxkit, cgroup v2), а не хоста и не Debian. `free`, `/proc/meminfo`, `dmesg` и значения `vm.*` в этих блоках описывают ВМ, у вас на сервере или ноутбуке они будут другими. PID и объёмы у вас будут другими, порядок событий тот же.

Рядом: [[Процессы в Linux]], [[Сигналы в Linux]], [[Память и сборка мусора]].

## Вопросы на собеседовании

1. [Чем VSZ отличается от RSS и почему VSZ огромный?](#vsz-и-rss)
2. [Почему «Linux съел всю память» и как читать `free`?](#page-cache-и-free)
3. [Что такое грязные страницы и когда нужен swap?](#грязные-страницы-swap-и-swappiness)
4. [Почему `free` в контейнере показывает память хоста?](#почему-free-в-контейнере-врёт)
5. [Почему `malloc` гигабайта не падает?](#overcommit)
6. [Как работает OOM killer и кого он убивает?](#oom-killer)

## VSZ и RSS

У каждого процесса своё виртуальное адресное пространство: процесс работает с адресами, а ядро через таблицы страниц отображает их на физические страницы (обычно по 4 КиБ) или не отображает вовсе. Страница физически появляется, когда процесс в неё впервые обращается.

- VSZ (`VmSize` в `/proc/<pid>/status`) — размер всего адресного пространства: всё, что процесс получил от ядра, включая то, что ни разу не трогал.
- RSS (`VmRSS`) — сколько из этого сейчас лежит в физической памяти. По man proc_pid_status(5), это сумма `RssAnon`, `RssFile` и `RssShmem`.

Поэтому VSZ у процесса большой и это нормально: пулы аллокатора, стеки потоков, подключённые библиотеки и зарезервированные области в VSZ учитываются сразу, а в RSS — только по мере использования. Общие страницы RSS считает целиком у каждого владельца; как делить их честно (`Pss`), показано в [[Процессы в Linux#Copy-on-write, память после fork]].

```python title="vsz-rss.py"
import mmap

SIZE = 512 * 1024 * 1024


def mem(label):
    with open("/proc/self/status") as f:
        v = {k: int(rest.split()[0]) // 1024
             for k, rest in (line.split(":") for line in f)
             if k in ("VmSize", "VmRSS")}
    print(f"{label:<20} VSZ {v['VmSize']:>4} МиБ  RSS {v['VmRSS']:>4} МиБ")


mem("старт")
m = mmap.mmap(-1, SIZE)
mem("после mmap 512 МиБ")
for off in range(0, SIZE // 2, mmap.PAGESIZE):
    m[off] = 1
mem("записали половину")
for off in range(SIZE // 2, SIZE, mmap.PAGESIZE):
    m[off] = 1
mem("записали всё")
m.close()
mem("после munmap")
```

```text title="Вывод"
старт                VSZ   13 МиБ  RSS    6 МиБ
после mmap 512 МиБ   VSZ  525 МиБ  RSS    7 МиБ
записали половину    VSZ  525 МиБ  RSS  263 МиБ
записали всё         VSZ  525 МиБ  RSS  519 МиБ
после munmap         VSZ   13 МиБ  RSS    7 МиБ
```

PID и объёмы у вас будут другими. `mmap` без записи увеличил VSZ на 512 МиБ, а RSS почти не изменился: страницы ещё не выданы. Запись в половину страниц подняла RSS на 256 МиБ, запись во все — на 512. После `munmap` обе цифры вернулись к исходным.

### Частые ошибки в ответе

- «VSZ — это сколько процесс занимает памяти». VSZ — размер адресного пространства. Реальную память показывает RSS, и то с оговоркой про общие страницы.
- «Большой VSZ — утечка». Рост VSZ без роста RSS ничего не стоит, пока страницы не тронуты. Смотреть надо на RSS и его динамику.
- «RSS всех процессов в сумме равен занятой памяти». Общие страницы (библиотеки, память после `fork`) входят в RSS каждого, поэтому сумма больше реальной.

## Page cache и free

Когда процесс читает файл, ядро кладёт прочитанные страницы в page cache: следующее чтение пойдёт из памяти. Эта память не потеряна: ядро отдаёт её под нужды процессов, как только они понадобятся. Поэтому у машины, которая давно работает, `free` почти всегда мал, а «занято под кэш» велико. Это признак нормальной работы, а не утечки.

`free` берёт цифры из `/proc/meminfo`. Колонки по man free(1):

- `free` — действительно неиспользуемая память (`MemFree`);
- `buff/cache` — page cache и буферы ядра (`Cached`, `Buffers`, `SReclaimable`);
- `available` — оценка, сколько памяти можно получить под новые приложения без swap (`MemAvailable`). Она учитывает, что большую часть кэша ядро освободит. Смотреть надо на неё, а не на `free`.

Демонстрация: файл на 400 МиБ пишется с `oflag=direct`, то есть мимо кэша, затем читается один раз. После чтения `free` упал примерно на размер файла, `buff/cache` вырос на столько же, а `available` почти не изменился. Затем файл удаляется, и его страницы исчезают из кэша сами, без всякого `drop_caches`.

```sh title="page-cache.sh"
trap 'rm -f /root/pc.bin' EXIT
dd if=/dev/urandom of=/root/pc.bin bs=1M count=400 oflag=direct status=none
echo "== файл записан в обход кэша =="
free -m
cat /root/pc.bin > /dev/null
echo "== файл прочитан один раз =="
free -m
rm /root/pc.bin
echo "== файл удалён =="
free -m
```

```text title="Вывод"
== файл записан в обход кэша ==
               total        used        free      shared  buff/cache   available
Mem:            7935         717        6817           1         566        7218
Swap:           1023          89         934
== файл прочитан один раз ==
               total        used        free      shared  buff/cache   available
Mem:            7935         718        6416           1         966        7216
Swap:           1023          89         934
== файл удалён ==
               total        used        free      shared  buff/cache   available
Mem:            7935         756        6778           1         566        7178
Swap:           1023          89         934
```

PID и объёмы у вас будут другими, как и исходное состояние кэша: цифры описывают ВМ Docker Desktop, а не вашу машину. Сравнивать нужно строки между собой: `free` после чтения меньше на ~400, `buff/cache` больше на ~400, `available` на месте. После `rm` кэш вернулся к исходному.

Про `drop_caches`. Запись в `/proc/sys/vm/drop_caches` сбрасывает чистый кэш (man proc_sys_vm(5)), но это приём для воспроизводимых замеров, например холодного кэша в бенчмарке. Лечением он не является: документация ядра прямо пишет, что файл не предназначен для управления ростом кэшей, ядро освобождает их само, когда память нужна, а использование файла может привести к проблемам с производительностью. После сброса приложение снова читает с диска, и становится хуже. Здесь он не нужен, и в блоках его нет.

### Частые ошибки в ответе

- «`free` почти ноль, значит память кончилась». Смотреть на `available`: если он большой, память есть, её держит кэш.
- «Page cache — утечка, надо освободить через `drop_caches`». Это не утечка, и сброс кэша на проде не лечит ничего: он выбрасывает полезные данные и бьёт по скорости. `drop_caches` годится для замеров, а не для эксплуатации.
- «`available` — это то же самое, что `free`». `available` включает ту часть кэша, которую ядро освободит под процессы.
- «Прочитанный файл занимает память навсегда». Страницы кэша вытесняются сами при нехватке, а у удалённого файла исчезают сразу, что и показал прогон.

## Грязные страницы, swap и swappiness

В памяти процесса бывает два вида страниц. Анонимные не связаны с файлом (куча, стек, `mmap` без файла): вернуть их на диск можно только в swap. Файловые отражают содержимое файла, и, пока их не меняли, ядро в любой момент выбросит их и прочитает заново. Файловая страница, в которую записали, но которая ещё не доехала до диска, называется грязной: `Dirty` в `/proc/meminfo` — память, которая ждёт записи на диск (man proc_meminfo(5)). Грязную страницу сначала нужно записать, и лишь потом её можно освободить. `sync` принудительно записывает все.

```sh title="dirty-pages.sh"
trap 'rm -f /root/dirty.bin' EXIT
show() { grep -E '^(Dirty|Writeback):' /proc/meminfo | tr -s ' ' | paste -sd' '; }
echo "до записи:  $(show)"
dd if=/dev/zero of=/root/dirty.bin bs=1M count=300 status=none
echo "после dd:   $(show)"
sync
echo "после sync: $(show)"
```

```text title="Вывод"
до записи:  Dirty: 148 kB Writeback: 0 kB
после dd:   Dirty: 307348 kB Writeback: 0 kB
после sync: Dirty: 0 kB Writeback: 0 kB
```

Объёмы у вас будут другими, и ядро могло начать запись само до вызова `sync`. `dd` без `oflag=direct` вернулся, когда данные были в памяти, а на диск ещё не ушли: `Dirty` вырос на ~300 МБ. `sync` довёл их до диска, `Dirty` вернулся к нулю.

Swap — область на диске, куда ядро выгружает анонимные страницы, чтобы освободить память под нужное. `vm.swappiness` задаёт, насколько охотно ядро выгружает страницы: по man proc_sys_vm(5), чем выше значение, тем агрессивнее, по умолчанию 60. Это настройка склонности, а не запрет и не требование: swap не делает память резиновой, а процесс, который постоянно читает выгруженное, тормозит.

```sh title="swap-info.sh"
echo "vm.swappiness = $(cat /proc/sys/vm/swappiness)"
grep -E '^Swap(Total|Free):' /proc/meminfo
```

```text title="Вывод"
vm.swappiness = 60
SwapTotal:       1048572 kB
SwapFree:         956420 kB
```

Объёмы у вас будут другими, это swap ВМ Docker Desktop.

### Частые ошибки в ответе

- «Swap — это то же самое, что память». Это место на диске: обращение к выгруженной странице идёт через диск и заметно медленнее обращения к памяти.
- «Если swap используется, память кончилась». Занятый swap сам по себе ничего не говорит: тревожит постоянный обмен страницами с диском, а не занятое место.
- «`swappiness=0` отключает swap». Параметр меняет склонность ядра к выгрузке, а отключение swap делается иначе (`swapoff`, отсутствие раздела).
- «После `write` данные уже на диске». После `write` они в page cache и помечены грязными. На диск их записывает ядро позже, гарантия — `fsync` (подробнее — [[Ввод-вывод и сеть#write и fsync]]).

## Почему free в контейнере врёт

`free` читает `/proc/meminfo`, а этот файл ничего не знает про cgroup: он показывает память всей машины (в Docker — ВМ или хоста). Лимит контейнера живёт не там: он задаётся в cgroup файлом `memory.max`. Процесс может быть ограничен 100 МБ и при этом видеть в `free` все гигабайты. Это путает: приложение или инженер считают, что памяти много, а процесс упрётся в лимит cgroup и будет убит.

Здесь cgroup нужна только как средство изоляции демонстрации: механику `memory.max`, `memory.high` и иерархию разбирает [[Контейнеры изнутри#cgroups v2 и память]] (и [[Контейнеры изнутри#memory.high и memory.max]]). Нужные шаги для блоков с cgroup:

1. В cgroup пространстве имён песочницы уже лежат процессы. По правилу «no internal process constraint» из документации cgroup v2 контроллер `memory` нельзя включить для дочерних cgroup там, где у родителя есть собственные процессы, и запись `+memory` в `cgroup.subtree_control` в этой песочнице заканчивается `Device or resource busy`. Для настоящего корня cgroup на хосте это правило не действует (cgroup v2), но корень пространства имён cgroup в контейнере — обычная cgroup на хосте, а не настоящий корень, поэтому правило здесь работает. Поэтому блок переносит все процессы в соседнюю cgroup `box`.
2. Включает `memory` для дочерних cgroup и создаёт вложенную cgroup с `memory.max`.
3. По выходу (`trap`, в том числе при ошибке) убивает процессы вложенной cgroup (`cgroup.kill`), ждёт, пока она опустеет, удаляет её, выключает `memory` и возвращает процессы на место.

```sh title="free-cgroup.sh"
root=/sys/fs/cgroup
cleanup() {
  if [ -d "$root/demo-free" ]; then
    cg=$root/demo-free
    echo 1 > "$cg/cgroup.kill"
    for _ in $(seq 50); do grep -q . "$cg/cgroup.procs" || break; sleep 0.1; done
    rmdir "$cg"
  fi
  echo -memory > "$root/cgroup.subtree_control" 2>/dev/null
  for p in $(cat "$root/box/cgroup.procs" 2>/dev/null); do
    echo "$p" > "$root/cgroup.procs" 2>/dev/null
  done
  rmdir "$root/box" 2>/dev/null
}
trap cleanup EXIT
mkdir "$root/box"
for p in $(cat "$root/cgroup.procs"); do
  echo "$p" > "$root/box/cgroup.procs" 2>/dev/null
done
echo +memory > "$root/cgroup.subtree_control"
mkdir "$root/demo-free"
echo 100M > "$root/demo-free/memory.max"
sh -c 'echo $$ > /sys/fs/cgroup/demo-free/cgroup.procs
echo "cgroup: $(cat /proc/self/cgroup)"
free -m
echo -n "memory.max: "; cat /sys/fs/cgroup/demo-free/memory.max
grep MemTotal /proc/meminfo'
```

```text title="Вывод"
cgroup: 0::/demo-free
               total        used        free      shared  buff/cache   available
Mem:            7935         693        6841           1         566        7242
Swap:           1023          89         934
memory.max: 104857600
MemTotal:        8125932 kB
```

PID и объёмы у вас будут другими. Процесс в cgroup `/demo-free` с `memory.max` в 100 МБ (104857600 байт) запустил `free -m` и увидел всю память ВМ, потому что `/proc/meminfo` про лимит не знает. Лимит видно только в `memory.max` (и `memory.current` для текущего потребления). Блок убрал за собой: после него `demo-free` и `box` не остаются.

Что смотреть вместо `free` внутри контейнера: файлы cgroup (`memory.max`, `memory.current`, `memory.events`) или метрики оркестратора.

### Частые ошибки в ответе

- «`free` в контейнере показывает память контейнера». Он показывает память машины, на которой запущено ядро. Лимит cgroup он не учитывает.
- «Памяти много, значит OOM не будет». Лимит у контейнера свой, и процесс упрётся в него при любой свободной памяти хоста.
- «Если `free` врёт, то и `ps` врёт». Размеры процесса (`RSS`, `VSZ`) относятся к самому процессу и не зависят от `/proc/meminfo`; не знают про лимит cgroup именно общие цифры машины.

## Overcommit

`malloc` и `mmap` резервируют адресное пространство, а не физическую память: страницы выдаются по первому обращению. Поэтому можно «выделить» больше, чем есть в машине, а проблема наступит позже, на записи. Насколько щедро ядро выдаёт такие обещания, определяет `vm.overcommit_memory` (man proc_sys_vm(5)):

- `0` — эвристика: очевидно чрезмерные запросы отклоняются, умеренные проходят. По умолчанию;
- `1` — всегда разрешать, не проверяя, пока память физически не кончится;
- `2` — не разрешать сверх предела `CommitLimit` (swap плюс доля RAM по `vm.overcommit_ratio`).

В песочнице режим другой, чем по умолчанию, поэтому блок сначала читает его. `Committed_AS` в `/proc/meminfo` — сколько выделено процессами суммарно, даже если ещё не использовано.

```python title="overcommit.py"
import mmap


def meminfo(key):
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith(key + ":"):
                return int(line.split()[1]) / 1024**2
    raise KeyError(key)


def vm(key):
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith(key + ":"):
                return int(line.split()[1]) / 1024**2
    raise KeyError(key)


with open("/proc/sys/vm/overcommit_memory") as f:
    print("vm.overcommit_memory =", f.read().strip())
print(f"MemTotal ВМ:   {meminfo('MemTotal'):6.1f} ГиБ")
print(f"Committed_AS:  {meminfo('Committed_AS'):6.1f} ГиБ до mmap")

SIZE = 64 * 1024**3
m = mmap.mmap(-1, SIZE)
print(f"mmap {SIZE // 1024**3} ГиБ: успех")
print(f"Committed_AS:  {meminfo('Committed_AS'):6.1f} ГиБ после mmap")
print(f"VmSize:        {vm('VmSize'):6.1f} ГиБ")
print(f"VmRSS:         {vm('VmRSS'):6.3f} ГиБ")
m[0] = 1
print(f"VmRSS после записи в один байт: {vm('VmRSS'):.3f} ГиБ")
```

```text title="Вывод"
vm.overcommit_memory = 1
MemTotal ВМ:      7.7 ГиБ
Committed_AS:     1.3 ГиБ до mmap
mmap 64 ГиБ: успех
Committed_AS:    65.3 ГиБ после mmap
VmSize:          64.0 ГиБ
VmRSS:          0.007 ГиБ
VmRSS после записи в один байт: 0.007 ГиБ
```

Объёмы у вас будут другими. В песочнице `vm.overcommit_memory` равен 1, поэтому 64 ГиБ при 7–8 ГиБ памяти ВМ выдались без проверок. `Committed_AS` вырос на 64 ГиБ, RSS не вырос, а запись в один байт добавила одну страницу, а не гигабайты. Что было бы в режиме 0, этот прогон не показывает: по документации ядра (overcommit-accounting) там очевидные превышения отклоняются, а умеренные проходят. В блоке нет записи во все 64 ГиБ: она закончилась бы OOM killer, и тот мог бы выбрать жертвой не вашу программу.

Overcommit откладывает проблему, а не снимает её: если процессы потом реально запишут больше, чем есть, придёт OOM killer.

### Частые ошибки в ответе

- «Если `malloc` вернул адрес, память выделена». Выдан адрес. Страницы появятся при записи, и именно там возможен OOM.
- «Overcommit — это баг или утечка». Это штатное поведение (форк с copy-on-write, большие разреженные массивы, пулы аллокаторов).
- «Режим 2 решает проблему OOM». По документации ядра, в нём процесс скорее получит ошибку при выделении, чем будет убит при записи в страницы. Но это не гарантия: приложение должно уметь обработать отказ, а предел `CommitLimit` задаёт ядро, а не потребности программы.

## OOM killer

Когда памяти не хватает и ядро не может её освободить (кэш выброшен, swap кончился или ограничен), оно вызывает OOM killer: тот выбирает процесс и посылает ему `KILL` (так сделано в исходниках ядра, `mm/oom_kill.c`). `KILL` перехватить нельзя (man signal(7)), поэтому у процесса нет шанса среагировать, а код выхода равен 137 (см. [[Сигналы в Linux#Коды выхода 128 + N]]).

Выбор жертвы строится на оценке `oom_score` (man proc_pid_oom_score(5)): чем она выше, тем вероятнее выбор; основа оценки — объём памяти процесса. К ней добавляется `oom_score_adj` (его значение прибавляется к оценке), от -1000 до +1000 (man proc_pid_oom_score_adj(5)): -1000 равносилен отключению OOM-убийства для процесса, положительные значения делают его жертвой охотнее. OOM может быть общесистемным или внутри cgroup, когда достигнут её `memory.max`: тогда жертва выбирается только внутри этой cgroup, а в `memory.events` растут счётчики `max`, `oom` и `oom_kill` (документация cgroup v2).

Прогон ниже — в cgroup с `memory.max` в 100 МБ и `memory.swap.max` равным 0: без второго swap ВМ мог бы поглотить часть нагрузки, и срабатывание отложилось бы. Процесс выделяет по 10 МиБ и пишет в каждый кусок, пока его не убьют.

```sh title="oom-cgroup.sh"
root=/sys/fs/cgroup
cg=$root/demo-oom
cleanup() {
  if [ -d "$cg" ]; then
    echo 1 > "$cg/cgroup.kill"
    for _ in $(seq 50); do grep -q . "$cg/cgroup.procs" || break; sleep 0.1; done
    rmdir "$cg"
  fi
  echo -memory > "$root/cgroup.subtree_control" 2>/dev/null
  for p in $(cat "$root/box/cgroup.procs" 2>/dev/null); do
    echo "$p" > "$root/cgroup.procs" 2>/dev/null
  done
  rmdir "$root/box" 2>/dev/null
  rm -f /tmp/hog.py
}
trap cleanup EXIT
mkdir "$root/box"
for p in $(cat "$root/cgroup.procs"); do
  echo "$p" > "$root/box/cgroup.procs" 2>/dev/null
done
echo +memory > "$root/cgroup.subtree_control"
mkdir "$cg"
echo 100M > "$cg/memory.max"
echo 0 > "$cg/memory.swap.max"
cat > /tmp/hog.py <<'PY'
import os

pid = os.getpid()
for name in ("oom_score", "oom_score_adj"):
    print(name, open(f"/proc/{pid}/{name}").read().strip())
chunks = []
for i in range(1, 41):
    chunks.append(bytearray(b"x" * (10 * 1024 * 1024)))
    print(f"выделено {i * 10} МиБ")
PY
( sh -c 'echo $$ > '"$cg"'/cgroup.procs
exec python3 -u /tmp/hog.py'; exit $? ) 2>/dev/null
echo "код выхода: $?"
echo "== memory.events =="
cat "$cg/memory.events"
echo "== dmesg =="
dmesg | grep -iE 'oom-kill|out of memory' | tail -n 3 | cut -c1-89 | sed 's/$/…/'
```

```text title="Вывод"
oom_score 666
oom_score_adj 0
выделено 10 МиБ
выделено 20 МиБ
выделено 30 МиБ
выделено 40 МиБ
выделено 50 МиБ
выделено 60 МиБ
выделено 70 МиБ
выделено 80 МиБ
код выхода: 137
== memory.events ==
low 0
high 0
max 35
oom 1
oom_kill 1
oom_group_kill 0
== dmesg ==
[135268.200448] python3 invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL), order=0, oom_scor…
[135268.200520] oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=1e4d8abaebc6e…
[135268.200544] Memory cgroup out of memory: Killed process 90655 (python3) total-vm:1157…
```

PID и объёмы у вас будут другими. Процесс добрался примерно до 80 МиБ и был убит: код выхода 137 (128 + 9), в `memory.events` `oom_kill` равен 1, а `max` показывает, сколько раз использование упиралось в предел и ядро пыталось освободить память. В `dmesg` видна строка `invoked oom-killer`, строка `oom-kill:constraint=CONSTRAINT_MEMCG` (OOM вызван лимитом cgroup, а не нехваткой памяти ВМ) и `Memory cgroup out of memory: Killed process …`. PID в этой строке ядро печатает в глобальном пространстве имён (`task_pid_nr` в `mm/oom_kill.c`), поэтому он не совпадает с PID внутри контейнера. Не забудьте, что `dmesg` общий для всей ВМ: в нём могут быть старые записи, поэтому берутся последние. Значение `oom_score` у процесса — 666 при нулевой `oom_score_adj`, хотя он занимает почти ничего. Это не ошибка: файл `/proc/<pid>/oom_score` показывает оценку, пересчитанную в диапазон от 0 до 2000, а потом умноженную на 2/3 (`proc_oom_score` в `fs/proc/base.c`): `(1000 + badness * 1000 / totalpages) * 2 / 3`. Без памяти и при `oom_score_adj` 0 это `1000 * 2 / 3`, то есть около 666, а при `oom_score_adj` 1000 около 1333 и чуть больше. Поэтому абсолютное число в `oom_score` читать как долю памяти нельзя, сравнивают оценки разных процессов между собой.

Теперь выбор жертвы. В cgroup работают два процесса: `keeper` держит 40 МиБ, но у него `oom_score_adj` равен 1000; `hog` держит 55 МиБ с `oom_score_adj` равным 0. Лимит сначала 200 МиБ, и оба спокойно помещаются, а когда оба загружены, блок опускает `memory.max` до 80 МиБ: тогда сумма не помещается. Так момент срабатывания не зависит от скорости, с которой процессы выделяют память. Кого убило, видно по строкам `Killed process` в `dmesg`; блок берёт только строки новее последней, что была до его запуска (метка берётся из самого `dmesg`, поэтому часы те же).

```sh title="oom-victim.sh"
root=/sys/fs/cgroup
cg=$root/demo-victim
cleanup() {
  if [ -d "$cg" ]; then
    echo 1 > "$cg/cgroup.kill"
    for _ in $(seq 50); do grep -q . "$cg/cgroup.procs" || break; sleep 0.1; done
    rmdir "$cg"
  fi
  echo -memory > "$root/cgroup.subtree_control" 2>/dev/null
  for p in $(cat "$root/box/cgroup.procs" 2>/dev/null); do
    echo "$p" > "$root/cgroup.procs" 2>/dev/null
  done
  rmdir "$root/box" 2>/dev/null
  rm -f /tmp/worker.py
}
trap cleanup EXIT
mkdir "$root/box"
for p in $(cat "$root/cgroup.procs"); do
  echo "$p" > "$root/box/cgroup.procs" 2>/dev/null
done
echo +memory > "$root/cgroup.subtree_control"
mkdir "$cg"
echo 200M > "$cg/memory.max"
echo 0 > "$cg/memory.swap.max"
cat > /tmp/worker.py <<'PY'
import os
import sys
import time

name, adj, mib, step = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
open("/sys/fs/cgroup/demo-victim/cgroup.procs", "w").write(str(os.getpid()))
open("/proc/self/oom_score_adj", "w").write(adj)
chunks = []
for _ in range(mib // 10):
    chunk = bytearray(10 * 1024 * 1024)
    chunk[::4096] = b"x" * len(range(0, len(chunk), 4096))
    chunks.append(chunk)
    time.sleep(step / 1000)
score = open("/proc/self/oom_score").read().strip()
print(f"{name}: oom_score_adj={adj}, score={score}, держит {mib} МиБ")
time.sleep(30)
PY
start=$(dmesg | tail -n 1 | awk '{gsub(/[][]/, "", $1); print $1}')
python3 -u /tmp/worker.py keeper 1000 40 0 &
disown
python3 -u /tmp/worker.py hog 0 55 100 &
disown
sleep 3
echo "== memory.max: 200M -> 80M =="
echo 80M > "$cg/memory.max"
sleep 1
echo "== убитые процессы по порядку =="
dmesg |
  awk -v t="$start" '{ts = $1; gsub(/[][]/, "", ts); if (ts + 0 > t) print}' |
  grep 'Killed process' |
  sed -E 's/.*\(([[:alnum:]_]+)\) .*(anon-rss:[0-9]+kB).*(oom_score_adj:.*)/\1 \2 \3/'
```

```text title="Вывод"
keeper: oom_score_adj=1000, score=1336, держит 40 МиБ
hog: oom_score_adj=0, score=670, держит 55 МиБ
== memory.max: 200M -> 80M ==
== убитые процессы по порядку ==
python3 anon-rss:43616kB oom_score_adj:1000
python3 anon-rss:53800kB oom_score_adj:0
```

PID и объёмы у вас будут другими. Первой убита запись с `oom_score_adj:1000`, то есть `keeper`, хотя он занимал меньше (около 43 МБ против 54 МБ у `hog`): его `oom_score_adj` сделал оценку выше. Так это выглядело во всех прогонах. Обычно второй записью ядро убивало и `hog` (в одном из шести прогонов второй записи не было). В `memory_max_write` (`mm/memcontrol.c`) при понижении `memory.max` цикл вызывает OOM killer снова и снова, пока использование памяти cgroup выше нового лимита. Убитый процесс отдаёт память не мгновенно, поэтому повторная проверка могла застать её ещё занятой; это вывод из кода, трассировкой он не проверялся. Поэтому вывод показывает порядок выбора жертвы, а не число убийств. Блок убрал cgroup вместе с остатками процессов (`cgroup.kill`).

На практике: `oom_score_adj` = -1000 для важного процесса (например, базы данных) защищает его, но при нехватке памяти убивать придётся другие процессы. Это не лечение: чинят причину (утечку, лимит, размер буферов), а не отключают OOM killer.

У контейнера OOM внутри его cgroup выглядит так же, как в прогоне выше: процесс убит `KILL`, код 137. Как это показывает оркестратор, в песочнице не воспроизводится.

### Частые ошибки в ответе

- «OOM killer убивает самый большой процесс». Он убивает процесс с наивысшей оценкой: размер её основа, но `oom_score_adj` её сдвигает, как показал `keeper`.
- «OOM killer можно отключить, и тогда всё будет работать». Если памяти реально нет, ядру придётся что-то сделать: убить процесс или отказать. Отключать OOM killer или ставить -1000 процессам не лечит нехватку, а перекладывает её на другие процессы. Исправлять нужно причину.
- «Код 137 — всегда OOM». 137 значит «убит `KILL`»; OOM killer шлёт `KILL`, но и `docker stop` по истечении срока тоже. Причину ищут в `dmesg` и событиях контейнера.
- «OOM в контейнере уронит хост». Если лимит cgroup достигнут, ядро убивает процесс в этой cgroup и не трогает остальные (документация cgroup v2).
- «`drop_caches` предотвращает OOM». Он сбрасывает только чистый кэш (man proc_sys_vm(5)); нехватку анонимной памяти процессов он не лечит.

## Что спросят дальше

- Как Python держит память: пулы, арены, почему RSS не падает после `del` — [[Память и сборка мусора]].
- Что такое `memory.high` и чем он отличается от `memory.max`, как cgroup ограничивает память контейнера — [[Контейнеры изнутри#memory.high и memory.max]].
- Что такое huge pages и transparent huge pages.
- Что такое `mlock` и когда процесс просит не выгружать его память.
- Как отличить утечку от роста кэша по `memory.stat` и `/proc/<pid>/smaps_rollup`.
- Что такое `vmstat`, PSI (`/proc/pressure/memory`) и как ловить нехватку памяти до OOM.
