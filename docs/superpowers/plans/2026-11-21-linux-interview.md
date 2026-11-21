# Linux к собеседованию — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить рубрику меню верхнего уровня «Linux» из шести страниц и указателя: процессы, сигналы, память, файлы и дескрипторы, ввод-вывод и сеть, контейнеры изнутри. Каждая демонстрация прогоняется в песочнице Debian через инструмент в репозитории.

**Architecture:** Страницы — заметки Obsidian в `Linux/`. Пункты меню — в `sidebar` в `.site/astro.config.mjs`. Демонстрации живут только в `.md`, у каждой есть уникальный на странице `title`. Скрипт `.site/scripts/linux-box.sh` собирает образ, поднимает контейнер с `--privileged` и прогоняет в нём блок страницы по `title` через `bash` или `python3`. Вывод вставляется в страницу вручную.

**Tech Stack:** Obsidian markdown, Astro Starlight, Docker Desktop (ядро linuxkit 6.12, cgroups v2), `debian:bookworm-slim`, bash, Python 3.11 из bookworm.

**Spec:** `docs/superpowers/specs/2026-11-21-linux-interview-design.md`. Исполнитель читает его целиком: состав каждой страницы перечислен там и здесь не повторяется.

## Global Constraints

- Папка `Linux/` в корне vault. Группа меню `{ label: 'Linux', items: pages('Linux', [...]) }` идёт в `sidebar` после группы `'Бэкенд'`. Итоговый порядок: `['Linux', 'Процессы в Linux', 'Сигналы в Linux', 'Память в Linux', 'Файлы и дескрипторы', 'Ввод-вывод и сеть', 'Контейнеры изнутри']`. Каждая задача добавляет только свою страницу, группу создаёт задача 1.
- Имена файлов ровно такие: `Linux.md`, `Процессы в Linux.md`, `Сигналы в Linux.md`, `Память в Linux.md`, `Файлы и дескрипторы.md`, `Ввод-вывод и сеть.md`, `Контейнеры изнутри.md`. Frontmatter `title` совпадает с именем файла.
- Формат страницы как у `Бэкенд/Базы данных/SQL-рецепты/Пожар в PostgreSQL.md`. Порядок частей:
  - frontmatter `title`;
  - абзац «о чём»;
  - строка `Рядом:`;
  - `## Вопросы на собеседовании` — список вопросов, каждый ссылается на свой раздел;
  - разделы `##`, в каждом механизм, демонстрация и «Частые ошибки в ответе»;
  - последним — `## Что спросят дальше`.

  У указателя эта схема своя, см. задачу 7.
- Запускаемая демонстрация оформляется блоком ```` ```sh title="<имя>.sh" ```` или ```` ```python title="<имя>.py" ````. `title` уникален на странице. Сразу под блоком идёт вывод в ```` ```text title="Вывод" ````. Фрагменты кода без `title` не запускаются, их мало, и это сказано словами.
- Каждый вывод получен через `make linux-block`. Изменчивые значения (PID, адреса, объёмы, время) помечены строкой под блоком: «PID и объёмы у вас будут другими». Если блок требует свежей песочницы, над ним написано «на свежей песочнице (`make linux-down && make linux-up`)».
- Строка вывода — не шире ~90 символов. Колонки `ps`, `ss`, `free` задаются явно, например `ps -o pid,ppid,stat,comm`. Обрезанное помечается `…`.
- Заголовки разделов — без обратных кавычек, двоеточий и «…».
- Каждое неочевидное утверждение о ядре и утилитах сверено с man7.org или docs.kernel.org. Не сверилось — на страницу не попадает. Список сверенных утверждений с источником исполнитель кладёт в отчёт задачи, на страницу его не переносит.
- Каждое утверждение из «Анти-целей» спеки, относящееся к теме страницы, опровергнуто в «Частых ошибках в ответе» этой страницы.
- Страница — до 500 строк.
- Правила для исполнителя: `~/.agents/rules/shell-core.md` (скрипт и sh-демонстрации), `python-core.md` (Python-демонстрации).
- Одна задача — один коммит в `main` с подписью `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. `.obsidian/` не коммитить. Пушит пользователь.

## Review Focus

1. **Python-демонстрация с `fork` печатает строку дважды.** Когда stdout — канал, буфер копируется в ребёнка. Задача 1: `python3` запускается с `-u`, тест `fork-no-dup`.
2. **Демонстрация зависает** (ждёт сигнала, `sleep` без конца), и `make linux-block` висит. Задача 1: таймаут `LINUX_BOX_TIMEOUT` (по умолчанию 120 с), тест `timeout`.
3. **Состояние утекает между блоками:** фоновые процессы, созданные cgroup, изменённые лимиты. Следующий блок даёт другой вывод. Задачи 2–6: блок либо убирает за собой (`kill`, `rmdir` cgroup), либо помечен «на свежей песочнице». После задачи `make linux-down`, и `docker ps -aq --filter name=reference-linux` пусто.
4. **Вывод читатель примет за общий случай, а он от окружения.** `free` и `/proc/meminfo` в контейнере показывают память ВМ, а не лимит cgroup. Ядро — linuxkit. `nproc` — ядра ВМ. Задача 3 показывает расхождение `free` и `memory.max` как отдельный вопрос («почему `free` в контейнере врёт»). Задача 6 подписывает окружение.
5. **Неверный факт проходит ревью.** Ревью на соответствие спеке не проверяет, верно ли объяснение. Каждая задача-страница кладёт в отчёт список сверенных утверждений с источниками, ревьюер выборочно сверяет три из них.

---

### Общие шаги каждой задачи-страницы

Задачи 2–6 идут по одной схеме. В самих задачах указано только своё.

- [ ] **Поднять песочницу:** `make linux-up`. Ожидается последняя строка `ready: reference-linux`.
- [ ] **Написать страницу** по спеке (раздел «Состав», пункт с номером страницы) и по списку «Обязательно на странице» из задачи.
- [ ] **Прогон:** каждый блок — `make linux-block PAGE="Linux/<страница>.md" TITLE="<имя>"`, вывод — в блок «Вывод». Код выхода 0, если демонстрация не показывает ненулевой код намеренно (тогда это сказано в тексте).
- [ ] **Меню:** добавить страницу в группу «Linux».
- [ ] **Сборка:** `make check` — выход 0, без ошибок `check-vault` и `check-dist`.
- [ ] **Телефон:** `make check-browser`. Новой страницы нет среди нарушений.
- [ ] **Убрать за собой:** `make linux-down`, `docker ps -aq --filter name=reference-linux` пусто.
- [ ] **Коммит:** `git add "Linux/<страница>.md" .site/astro.config.mjs && git commit`.

---

### Task 1: Песочница и «Процессы в Linux»

Риск: обычный — локальный скрипт и документация, ошибка видна сразу при прогоне.

**Files:**
- Create: `.site/linux-box/Dockerfile`
- Create: `.site/scripts/linux-box.sh`
- Create: `.site/scripts/linux-box.test.sh`
- Modify: `Makefile` — цели `linux-up`, `linux-block`, `linux-shell`, `linux-down`, `linux-test`, все в `.PHONY`
- Create: `Linux/Процессы в Linux.md`
- Modify: `.site/astro.config.mjs` — новая группа `'Linux'` после `'Бэкенд'` с одной страницей

**Interfaces:**
- Produces (задачи 2–6 пользуются только этим):
  - Образ `reference-linux:bookworm` из `.site/linux-box/Dockerfile` на `debian:bookworm-slim`. Пакеты: `procps psmisc strace util-linux iproute2 lsof python3 curl sysstat tini busybox-static`, после установки `rm -rf /var/lib/apt/lists/*`.
  - `make linux-up` — собирает образ (кеш Docker), пересоздаёт контейнер `${LINUX_BOX_CONTAINER:-reference-linux}`: `docker run -d --privileged --cgroupns=private --hostname reference-linux --name … reference-linux:bookworm tini -- sleep infinity`. Последняя строка вывода — `ready: <имя контейнера>`.
  - `make linux-block PAGE="<путь .md>" TITLE="<имя>"` — находит на странице ровно один блок, у которого после ограждения ```` ``` ```` стоит `sh` или `python` и `title="<имя>"`. `sh` выполняется через `docker exec -i -w /root <контейнер> timeout <T> bash -s`, `python` — через `… timeout <T> python3 -u -`. `T` = `${LINUX_BOX_TIMEOUT:-120}`. Код выхода скрипта — код выхода блока. Ошибки:
    - блока нет — выход 1, в stderr `block not found: <имя>`;
    - блоков два и больше, даже на разных языках, — выход 1, `block not unique: <имя>`;
    - сработал таймаут (124) — выход 124, `block timed out: <имя>`;
    - контейнер не запущен — выход 1, `not running: <имя контейнера>; run make linux-up`.
  - `make linux-shell` — `docker exec -it <контейнер> bash`.
  - `make linux-down` — удаляет контейнер. Если его нет — выход 0.
  - `make linux-test` — `bash .site/scripts/linux-box.test.sh`.
- Разбор блока устроен как `extract_block` в `.site/scripts/sql-pg.sh`: заголовок передаётся через `ENVIRON`, `\r` и концевые пробелы срезаются для сравнения, тело печатается без `\r`. `sql-pg.sh` не трогать.

- [ ] **Шаг 1: тест до кода.** `.site/scripts/linux-box.test.sh` поднимает свой контейнер `LINUX_BOX_CONTAINER=reference-linux-test-$$`, по `trap` удаляет его и временные `.md`. Проверки:
  - `not-found` — `TITLE=nope.sh` → код 1, stderr содержит `block not found: nope.sh`;
  - `not-unique` — `sh` и `python` блоки с одним `title="dup"` → код 1, `block not unique: dup`;
  - `sh-runs` — блок `echo hi; id -u` → stdout `hi` и `0`;
  - `exit-code` — блок `exit 3` → код 3;
  - `fork-no-dup` — Python-блок: `print("before")`, затем `os.fork()`, ребёнок `sys.exit(0)`, родитель `os.waitpid` и `print("after")` → `before` в выводе ровно один раз, `after` есть;
  - `privileged` — блок `unshare --pid --fork --mount-proc sh -c 'echo $$'` → stdout `1`;
  - `timeout` — `LINUX_BOX_TIMEOUT=2`, блок `sleep 10` → код 124, `block timed out`;
  - `crlf` — страница с окончаниями `\r\n`, блок находится и выполняется;
  - `down-twice` — `linux-down` дважды → оба раза код 0;
  - `not-running` — `block` после `down` → код 1, `not running`.
- [ ] **Шаг 2:** `make linux-test` падает: скрипта нет.
- [ ] **Шаг 3:** написать `Dockerfile`, `linux-box.sh` (bash, `set -euo pipefail`, подкоманды `up|block|shell|down`) и цели `Makefile`.
- [ ] **Шаг 4:** `make linux-test` — все 10 проверок проходят. Второй прогон подряд тоже проходит.
- [ ] **Шаг 5: страница «Процессы в Linux».** Состав — спека, пункт 1. Обязательно на странице:
  - copy-on-write прогоном: RSS и общие страницы ребёнка (`/proc/<pid>/smaps_rollup`: `Rss`, `Shared_*`, `Private_*`) сразу после `fork` и после записи в большой буфер;
  - зомби прогоном: состояние `Z` в `ps`, `kill -9` его не убирает, после `wait` родителя зомби нет;
  - сирота: новый PPID после смерти родителя. Внутри песочницы под `tini` и внутри `unshare --pid --fork` — кто усыновляет;
  - PID 1 без сбора зомби: процесс под PID 1 в `unshare --pid --fork`, который не делает `wait`, копит зомби. Тот же сценарий под `tini` — не копит;
  - состояния R, S, D, Z, T, у каждого — когда процесс в нём оказывается. D и T — демонстрация, если воспроизводится, иначе текстом с пометкой;
  - ссылка на «Потоки и процессы» вместо пересказа GIL.
- [ ] **Шаг 6:** общие шаги «Прогон», «Меню», «Сборка», «Телефон», «Убрать за собой». В коммит идут также `Dockerfile`, скрипт, тест и `Makefile`.

---

### Task 2: Сигналы в Linux

Риск: высокий — совет по graceful shutdown переносится в прод, ошибка (потерянный `TERM` под PID 1) молча теряет запросы.

**Files:** Create `Linux/Сигналы в Linux.md`; Modify `.site/astro.config.mjs`.

**Interfaces:** Consumes — `make linux-*` из задачи 1. Produces — заголовки разделов для указателя, раздел про PID 1 и сигналы (на него ссылаются задачи 1 и 6).

**Обязательно на странице:**
- `KILL` и `STOP` нельзя перехватить: прогон — `signal.signal(SIGKILL, …)` даёт `OSError`.
- Коды выхода 130, 137, 143 прогоном: `sleep`, убитый `INT`, `KILL`, `TERM`, и `echo $?`.
- Python-обработчик `TERM`: синхронный (`signal.signal`) и asyncio (`loop.add_signal_handler`), с дожатием текущей работы. Вывод показывает, что работа доделана.
- PID 1: `kill -TERM 1` внутри `unshare --pid --fork` процессу без обработчика ничего не делает, а тот же процесс не под PID 1 умирает. Shell-форма `CMD` (`sh -c "python app.py"`): `TERM` получает `sh`, а не приложение. Показать через `sh -c` под PID 1 в `unshare`.
- Kubernetes при остановке пода: `preStop` → `TERM` → `terminationGracePeriodSeconds` (по умолчанию 30 с) → `KILL`. Значение сверить с документацией kubernetes.io. Кластер не поднимаем: это текст и mermaid-диаграмма последовательности, помеченные «не воспроизводится здесь».
- Сигнал группе процессов: `kill -- -PGID` против `kill PID`, дети, которые остаются жить.

- [ ] Общие шаги (см. выше).

---

### Task 3: Память в Linux

Риск: высокий — совет про page cache и OOM переносится в прод (`drop_caches`, отключение OOM killer).

**Files:** Create `Linux/Память в Linux.md`; Modify `.site/astro.config.mjs`.

**Interfaces:** Consumes — задача 1. Produces — раздел про OOM killer, на него ссылается задача 6.

**Обязательно на странице:**
- VSZ и RSS одного процесса до и после `mmap` или выделения без записи и после записи.
- Page cache: `free -m` до и после чтения файла на 200–500 МБ (`dd` из `/dev/urandom` в файл, затем `cat > /dev/null`). `available` почти не изменился, `buff/cache` вырос.
- «`free` в контейнере врёт»: процесс во вложенной cgroup с `memory.max` = 100 МБ запускает `free -m` и видит всю память ВМ, а `cat memory.max` показывает лимит. Пояснение: `free` читает `/proc/meminfo`, а он не знает про cgroup.
- Overcommit: Python выделяет больше памяти ВМ без записи — успех; `vm.overcommit_memory` читается из `/proc/sys/vm`.
- OOM killer прогоном. Процесс выедает память во вложенной cgroup с `memory.max` (чтобы не задеть ВМ). В `dmesg` строка `Out of memory` / `oom-kill`, в `memory.events` счётчик `oom_kill`. `oom_score` и `oom_score_adj` процесса. Cgroup убирается за собой.
- `drop_caches` — только в «Частых ошибках» как приём для замеров, не лечение.

- [ ] Общие шаги (см. выше).

---

### Task 4: Файлы и дескрипторы

Риск: обычный — локальные демонстрации, советы без необратимых действий.

**Files:** Create `Linux/Файлы и дескрипторы.md`; Modify `.site/astro.config.mjs`.

**Interfaces:** Consumes — задача 1. Produces — раздел про лимит дескрипторов, на него ссылается задача 6.

**Обязательно на странице:**
- Inode, жёсткая и символическая ссылки: `ls -li`, `stat`. Удаление оригинала — жёсткая ссылка жива, символическая висит.
- `/proc/<pid>/fd` процесса Python, который открыл файл и сокет. Наследование дескриптора после `fork`.
- «Too many open files»: `ulimit -n 64` в подоболочке, Python открывает файлы до `OSError: [Errno 24]`. Мягкий и жёсткий лимит (`ulimit -Sn`, `-Hn`).
- Удалённый открытый файл: `df` до, во время и после закрытия; `lsof +L1` находит его. Место возвращается только после закрытия.
- Кончились inode при свободном месте — `df -i` на маленькой tmpfs (`mount -t tmpfs -o size=10M,nr_inodes=100`), прогоном.
- Права и `umask`: файл, созданный при `umask 022` и `077`.

- [ ] Общие шаги (см. выше).

---

### Task 5: Ввод-вывод и сеть

Риск: обычный — демонстрации на чтение, ошибка в объяснении ловится ревью по сверенным утверждениям.

**Files:** Create `Linux/Ввод-вывод и сеть.md`; Modify `.site/astro.config.mjs`.

**Interfaces:** Consumes — задача 1.

**Обязательно на странице:**
- `strace -c` на чтении файла по 1 байту и по 64 КБ: число `read` и время.
- `strace -f -e trace=epoll_wait,epoll_ctl` на минимальном asyncio-сервере или `asyncio.sleep`. Видно `epoll_wait`. Ссылка на «asyncio под капотом».
- `ss -tlnp` с Python-сервером на порту. Связь сокета с дескриптором в `/proc/<pid>/fd`.
- `TIME_WAIT` прогоном: несколько коротких соединений к локальному серверу, `ss -tan state time-wait`. Видно, на какой стороне он остаётся: у того, кто закрыл первым.
- Load average: `uptime` до и во время нагрузки на CPU (N процессов `yes > /dev/null` на 30–60 с). Пояснение, что в счёт идут R и D. Процессы убираются за собой.

- [ ] Общие шаги (см. выше).

---

### Task 6: Контейнеры изнутри

Риск: высокий — советы про лимиты CPU и памяти в Kubernetes и про `--privileged` переносятся в прод.

**Files:** Create `Linux/Контейнеры изнутри.md`; Modify `.site/astro.config.mjs`.

**Interfaces:** Consumes — задача 1; ссылки на разделы задач 2 (PID 1), 3 (OOM), 4 (лимит дескрипторов).

**Обязательно на странице:**
- Окружение подписано в начале страницы: Docker Desktop, ядро linuxkit, cgroups v2, `--privileged` только для демонстрации.
- `unshare --pid --uts --mount --net --fork --mount-proc`: PID 1, свой hostname, только `lo` в `ip link`. `ls -l /proc/<pid>/ns` у двух процессов. `nsenter` в созданный namespace.
- cgroups v2: дочерняя cgroup в `/sys/fs/cgroup`, `memory.max`, процесс в `cgroup.procs`, OOM внутри неё (`memory.events`). `cpu.max` = `50000 100000`, нагрузка, `cpu.stat`: `nr_throttled` и `throttled_usec` растут. Cgroup убирается за собой.
- overlayfs руками: `mount -t overlay` с `lowerdir`, `upperdir`, `workdir` на tmpfs. Запись в файл нижнего слоя → копия в `upperdir`. Удаление → whiteout.
- «Контейнер своими руками»: rootfs на tmpfs из `/bin/busybox` (пакет `busybox-static` из задачи 1) и ссылок на него, затем `unshare` + cgroup + `pivot_root` (или `chroot`, если `pivot_root` не выходит — причина в отчёт). Вывод: внутри PID 1, свой hostname, лимит памяти.
- `docker run` под капотом (CLI → dockerd → containerd → runc → `clone` → `pivot_root` → `execve`), OCI. Текст и mermaid, помеченные «не воспроизводится здесь», если песочница не может показать дерево процессов ВМ.

- [ ] Общие шаги (см. выше).

---

### Task 7: Указатель, оглавление и обратные ссылки

Риск: обычный — навигация, `make check` ловит битые ссылки.

**Files:**
- Create: `Linux/Linux.md`
- Modify: `.site/astro.config.mjs` — `'Linux'` первым в группе
- Modify: `index.md` — раздел `## Linux` после `## Бэкенд` и строка в «Как читать»
- Modify: строка `Рядом:` или «Что спросят дальше» в `Python/Конкурентность/Потоки и процессы.md`, `Python/Конкурентность/Память и сборка мусора.md`, `Python/Конкурентность/asyncio под капотом.md`, `Python/Конкурентность/Таймауты и нагрузка в asyncio.md`, `Бэкенд/Чек-лист бэкенда.md`

**Interfaces:** Consumes — заголовки разделов страниц 1–6.

**Обязательно:**
- `Linux.md` устроен как `Python/Python.md`: по каждому вопросу из блоков «Вопросы на собеседовании» страниц 1–6 — ответ на 30 секунд (2–4 предложения) со ссылкой `[[Страница#Раздел]]`. Демонстраций и «Что спросят дальше» нет.
- В `index.md` — одна строка на страницу, в стиле соседних разделов.
- Обратная ссылка ставится там, где она по смыслу: из «Потоков и процессов» — на «Процессы в Linux», из «Памяти и сборки мусора» — на «Память в Linux», из asyncio-страниц — на «Ввод-вывод и сеть», из «Чек-листа бэкенда» — на «Linux».
- `make check` и `make check-browser`, как в общих шагах.

- [ ] Написать, проверить, закоммитить одним коммитом.
