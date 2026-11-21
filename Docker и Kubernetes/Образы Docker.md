---
title: Образы Docker
---

Как устроен образ и как собрать его правильно: слои и то, что показывает `docker history`, кэш сборки и порядок инструкций, почему удалённый файл остаётся в образе, `.dockerignore`, многоэтапная сборка, `ENTRYPOINT` и `CMD`, пользователь не root, секреты при сборке, тег и digest. Каждая демонстрация собирает настоящий образ и показывает вывод Docker. Кластер не нужен.

Как читать. Блок запускается `make k8s-block PAGE="Docker и Kubernetes/Образы Docker.md" TITLE="<имя>"`, вывод ниже снят именно так (Docker Desktop 29.5, buildx 0.34, хранилище образов containerd, процессор arm64). Базовые образы закреплены по версии (`alpine:3.21`, `alpine:3.20.3`). Всё, что блок создаёт, называется с префиксом `ref-` и удаляется в конце блока. Размеры и digest зависят от платформы и версий: у вас будут другие числа, важны соотношения.

Рядом: [[Контейнеры изнутри]], [[Сигналы в Linux]], [[Деплой в Kubernetes]], [[Конфигурация и нагрузки в Kubernetes]].

## Вопросы на собеседовании

1. [Из чего состоит образ, какие инструкции Dockerfile дают слой и что показывает docker history?](#слои-и-docker-history)
2. [Как работает кэш сборки и почему зависимости копируют и ставят до кода?](#кэш-сборки)
3. [Файл удалили в следующем RUN, а образ не уменьшился: почему, и как уменьшить образ?](#размер-образа)
4. [Чем отличаются ENTRYPOINT и CMD, exec- и shell-форма?](#entrypoint-и-cmd)
5. [Почему процесс в контейнере не должен работать от root и как это настроить?](#пользователь-не-root)
6. [Как передать секрет при сборке и почему не через ARG и ENV?](#секреты-при-сборке)
7. [Чем тег отличается от digest и почему latest плох для деплоя?](#тег-и-digest)

## Слои и docker history

Образ — это набор слоёв только для чтения и конфиг (команда запуска, переменные окружения, пользователь, порты); контейнер — процесс плюс один записываемый слой сверху. Как слои склеиваются и что такое copy-up, разобрано в [[Контейнеры изнутри#Слои и overlayfs]]; здесь важно, откуда слои берутся при сборке.

По документации Docker, слой дают инструкции, которые меняют файлы: `RUN`, `COPY`, `ADD`, а `WORKDIR` создаёт каталог, если его нет. Остальные (`ENV`, `LABEL`, `EXPOSE`, `CMD`, `ENTRYPOINT`, `USER`) только пишут метаданные в конфиг образа, их размер в истории — `0B`. Блок собирает образ с каждым видом инструкций и показывает `docker history`.

```sh title="layers.sh"
cat > Dockerfile <<'EOF'
FROM alpine:3.21
ENV APP_ENV=demo
WORKDIR /app
COPY app.txt .
RUN echo built > /app/built.txt
LABEL purpose=demo
EXPOSE 8080
CMD ["cat", "/app/built.txt"]
EOF
echo hello > app.txt
docker build -q -t ref-layers:1 . > /dev/null
docker history --format 'table {{.Size}}\t{{.CreatedBy}}' ref-layers:1
docker rmi ref-layers:1 > /dev/null
```

```text title="Вывод"
SIZE      CREATED BY
0B        CMD ["cat" "/app/built.txt"]
0B        EXPOSE [8080/tcp]
0B        LABEL purpose=demo
12.3kB    RUN /bin/sh -c echo built > /app/built.txt #…
12.3kB    COPY app.txt . # buildkit
8.19kB    WORKDIR /app
0B        ENV APP_ENV=demo
0B        CMD ["/bin/sh"]
8.8MB     ADD alpine-minirootfs-3.21.10-aarch64.tar.gz…
```

Читать снизу вверх, как строились слои. Две нижние строки (`ADD alpine-minirootfs…`, `CMD ["/bin/sh"]`) — из базового образа, выше идут наши инструкции. `0B` у `ENV`, `LABEL`, `EXPOSE`, `CMD`: файлов они не меняют. Ненулевой размер у `RUN` и `COPY`, а у `WORKDIR` — тоже: он создал каталог `/app`. Размеры будут другими на другой платформе.

### Частые ошибки в ответе

- «Каждая инструкция Dockerfile создаёт слой с файлами». Файлы меняют `RUN`, `COPY`, `ADD` (и `WORKDIR`, создающий каталог); `ENV`, `LABEL`, `EXPOSE`, `CMD` пишут только метаданные.
- «`EXPOSE 8080` публикует порт». Нет: это подсказка тому, кто запускает контейнер; порт на хост выводит `-p` или `-P`, а в Kubernetes порт наружу задаёт Service.
- «Образ — это легковесная виртуальная машина». Образ — это файлы и конфиг; ядро у контейнера хозяйское, процесс запускается как обычный (см. [[Контейнеры изнутри]]).

## Кэш сборки

Перед шагом builder проверяет, есть ли готовый результат. Для `COPY` и `ADD` кэш считается по контрольной сумме файлов; для `RUN` — по тексту команды, и по документации Docker он не сбрасывается сам между сборками. Как только один шаг не попал в кэш, все следующие собираются заново. Отсюда порядок «сначала то, что меняется редко»: файл зависимостей, установка зависимостей, затем код.

Блок строит два образа из одних и тех же файлов. В «хорошем» `requirements.txt` копируется и «устанавливается» до кода, в «плохом» сначала `COPY .` целиком. Установка здесь имитирована (`echo`), чтобы блок не зависел от сети; на кэш это не влияет. Первая сборка идёт с `--no-cache`, вывод `--progress=plain` отфильтрован до шагов `COPY` и `RUN`: `CACHED` — шаг взят из кэша, `built` — выполнен.

```sh title="cache.sh"
mkdir app && cd app
echo "flask==3.0" > requirements.txt
echo "print('v1')" > main.py
cat > good.Dockerfile <<'EOF'
FROM alpine:3.21
COPY requirements.txt /app/
RUN echo "install $(cat /app/requirements.txt)" > /app/deps.txt
COPY main.py /app/
EOF
cat > bad.Dockerfile <<'EOF'
FROM alpine:3.21
COPY . /app/
RUN echo "install $(cat /app/requirements.txt)" > /app/deps.txt
EOF
build() {  # $1 - Dockerfile, остальное - флаги docker build
  f="$1"; shift
  docker build --progress=plain "$@" -f "$f" -t ref-cache:1 . 2>&1 | awk '
    /^#[0-9]+ \[[0-9]+\/[0-9]+\] (COPY|RUN)/ { n = $1; $1 = ""; s = substr($0, 2)
      if (length(s) > 60) s = substr(s, 1, 59) "…"
      name[n] = s; order[++c] = n }
    /^#[0-9]+ CACHED/ { cached[$1] = 1 }
    END { for (i = 1; i <= c; i++) print (cached[order[i]] ? "CACHED" : "built") " " name[order[i]] }' |
    sort -b -k2,2
}
echo "== хороший порядок, первая сборка с --no-cache"
build good.Dockerfile --no-cache
echo "== вторая сборка без изменений"
build good.Dockerfile
echo "== правка main.py"
echo "print('v2 $$')" > main.py
build good.Dockerfile
echo "== плохой порядок (COPY . до RUN): сборка, правка main.py, сборка"
build bad.Dockerfile --no-cache > /dev/null
echo "print('v3 $$')" > main.py
build bad.Dockerfile
docker rmi ref-cache:1 > /dev/null
```

```text title="Вывод"
== хороший порядок, первая сборка с --no-cache
built [2/4] COPY requirements.txt /app/
built [3/4] RUN echo "install $(cat /app/requirements.txt)" > /ap…
built [4/4] COPY main.py /app/
== вторая сборка без изменений
CACHED [2/4] COPY requirements.txt /app/
CACHED [3/4] RUN echo "install $(cat /app/requirements.txt)" > /ap…
CACHED [4/4] COPY main.py /app/
== правка main.py
CACHED [2/4] COPY requirements.txt /app/
CACHED [3/4] RUN echo "install $(cat /app/requirements.txt)" > /ap…
built [4/4] COPY main.py /app/
== плохой порядок (COPY . до RUN): сборка, правка main.py, сборка
built [2/3] COPY . /app/
built [3/3] RUN echo "install $(cat /app/requirements.txt)" > /ap…
```

После правки `main.py` в хорошем порядке пересобран только `COPY main.py`; в плохом `COPY . /app/` увидел изменённый файл, и вместе с ним заново прошла и установка зависимостей. На настоящем `pip install` это минуты вместо секунд.

### Частые ошибки в ответе

- «`RUN apt-get update` в кэше всегда свежий». Кэш `RUN` не сбрасывается сам: устаревший список пакетов остаётся, пока не пересоберёшь с `--no-cache` или не изменишь текст команды. Поэтому `apt-get update` и `install` пишут в одном `RUN`.

## Размер образа

Слой неизменяем: `rm` в следующем `RUN` не стирает файл из предыдущего слоя, а лишь прячет его в итоговой файловой системе (механизм whiteout — в [[Контейнеры изнутри#Слои и overlayfs]]). Байты остаются в образе и уходят в реестр. Блок строит два образа: файл в 50 МБ создаётся и удаляется в разных `RUN` и в одном. Данные случайные, чтобы сжатие не уменьшило их до нуля.

```sh title="delete-layer.sh"
cat > a.Dockerfile <<'EOF'
FROM alpine:3.21
RUN head -c 50000000 /dev/urandom > /big.bin
RUN rm /big.bin
EOF
cat > b.Dockerfile <<'EOF'
FROM alpine:3.21
RUN head -c 50000000 /dev/urandom > /big.bin && rm /big.bin
EOF
docker build -q -f a.Dockerfile -t ref-rm:separate . > /dev/null
docker build -q -f b.Dockerfile -t ref-rm:same . > /dev/null
for t in separate same; do
  echo "== $t: размер образа в байтах и два верхних слоя"
  docker image inspect --format '{{.Size}}' ref-rm:$t
  docker history --format 'table {{.Size}}\t{{.CreatedBy}}' ref-rm:$t | head -3
done
docker rmi ref-rm:separate ref-rm:same > /dev/null
```

```text title="Вывод"
== separate: размер образа в байтах и два верхних слоя
53992722
SIZE      CREATED BY
4.1kB     RUN /bin/sh -c rm /big.bin # buildkit
50MB      RUN /bin/sh -c head -c 50000000 /dev/urandom…
== same: размер образа в байтах и два верхних слоя
3976854
SIZE      CREATED BY
4.1kB     RUN /bin/sh -c head -c 50000000 /dev/urandom…
0B        CMD ["/bin/sh"]
```

Размер из `docker image inspect` (в байтах): у `separate` около 54 МБ, у `same` около 4 МБ — база без файла. В истории `separate` слой в 50 МБ стоит на месте, хотя следующий `RUN rm` отработал; у `same` слоя с файлом нет, `RUN` дал почти ноль. Числа зависят от платформы и хранилища образов, важно соотношение. Лекарство — создать и удалить в одном `RUN` (слоя с файлом не появится), а ещё лучше не класть лишнее в образ вовсе.

### Контекст сборки и dockerignore

Перед сборкой клиент отправляет builder'у контекст — каталог сборки. Файлы из `.dockerignore` в контекст не попадают, это ускоряет сборку (особенно с удалённым builder'ом) и не даёт случайно скопировать в образ `.env` или `.git`. Шаблоны — как в glob Go, `data/` и `data` равнозначны, `!` задаёт исключение; сами `Dockerfile` и `.dockerignore` отправляются builder'у всегда (нужны для сборки). `COPY . …` скопирует и `Dockerfile` в образ, если он не перечислен в `.dockerignore`: в блоке ниже он виден в `/app`.

Блок кладёт в контекст 30 МБ мусора и собирает образ с `COPY . /app/` без `.dockerignore` и с ним.

```sh title="dockerignore.sh"
mkdir ctx && cd ctx
mkdir data && head -c 30000000 /dev/urandom > data/dump.bin
echo "print('app')" > main.py
cat > Dockerfile <<'EOF'
FROM alpine:3.21
COPY . /app/
EOF
ctx_size() {
  docker build --progress=plain --no-cache -t ref-ctx:1 . 2>&1 | awk '
    /load build context/ { id = $1 }
    id != "" && $1 == id && /transferring context/ { $1 = ""; print "контекст:" $0 }'
}
echo "== без .dockerignore"
ctx_size
printf 'data/\n' > .dockerignore
echo "== с .dockerignore (data/)"
ctx_size
echo "== что попало в /app образа"
docker run --rm ref-ctx:1 ls /app
docker rmi ref-ctx:1 > /dev/null
```

```text title="Вывод"
== без .dockerignore
контекст: transferring context: 30.01MB 0.2s done
== с .dockerignore (data/)
контекст: transferring context: 101B done
== что попало в /app образа
Dockerfile
main.py
```

Время передачи у вас будет другим. Без `.dockerignore` на builder ушло 30 МБ, с ним — сто байт. Мусор из `data/` в образ не попал: `ls` показывает только `Dockerfile` и `main.py` (файл `.dockerignore` скрыт из-за точки в имени).

### Многоэтапная сборка

В многоэтапной сборке (`multi-stage`) инструменты сборки живут в одном этапе, а в итоговый образ `COPY --from` переносит только результат; в образ попадает только последний этап. Блок сравнивает «инструменты» в 40 МБ в одном этапе и в отдельном.

```sh title="multistage.sh"
cat > single.Dockerfile <<'EOF'
FROM alpine:3.21
RUN head -c 40000000 /dev/urandom > /toolchain.bin
RUN echo "result" > /result.txt
CMD ["cat", "/result.txt"]
EOF
cat > multi.Dockerfile <<'EOF'
FROM alpine:3.21 AS build
RUN head -c 40000000 /dev/urandom > /toolchain.bin
RUN echo "result" > /result.txt
FROM alpine:3.21
COPY --from=build /result.txt /result.txt
CMD ["cat", "/result.txt"]
EOF
docker build -q -f single.Dockerfile -t ref-ms:single . > /dev/null
docker build -q -f multi.Dockerfile -t ref-ms:multi . > /dev/null
for t in single multi; do
  printf '%s: ' "$t"
  docker image inspect --format '{{.Size}}' ref-ms:$t
done
docker run --rm ref-ms:multi
docker run --rm ref-ms:multi ls /toolchain.bin || echo "код выхода: $?"
docker rmi ref-ms:single ref-ms:multi > /dev/null
```

```text title="Вывод"
single: 43989911
multi: 3977052
result
ls: /toolchain.bin: No such file or directory
код выхода: 1
```

Размер из `docker image inspect` (в байтах): у `single` около 44 МБ, у `multi` около 4 МБ — как у базового образа. В нём нет `/toolchain.bin` (код выхода 1 из `ls` показан намеренно). В реальном проекте так отделяют компилятор и `-dev`-пакеты от рантайма. Числа зависят от платформы.

### Частые ошибки в ответе

- «`rm` в следующем `RUN` уменьшит образ». Слой с файлом остаётся; удалять надо в том же `RUN`, где файл создан, или собирать многоэтапно.
- «`.dockerignore` нужен только ради размера образа». Он сокращает контекст и защищает от случайного `COPY .env`; при точечных `COPY` размер образа от него не меняется.
- «`docker system prune -a` — безопасная уборка». Он удалит все образы без контейнеров, а не только висячие, и весь кэш сборки: следующая сборка пойдёт с нуля, а образы придётся скачивать заново. Остановленные контейнеры тоже пропадут. Сначала посмотрите `docker system df`, чистите точечно (`docker image rm`, `docker image prune`).

## ENTRYPOINT и CMD

`ENTRYPOINT` — что запускается, `CMD` — аргументы по умолчанию. Если задана exec-форма `ENTRYPOINT`, то аргументы `docker run` после имени образа заменяют `CMD`, а `ENTRYPOINT` остаётся. Без `ENTRYPOINT` аргументы заменяют всю команду из `CMD`. Заменить сам `ENTRYPOINT` можно флагом `--entrypoint`. У shell-формы (`ENTRYPOINT команда аргументы` без квадратных скобок) есть ловушка: команда запускается как `/bin/sh -c`, `CMD` и аргументы `docker run` ей не передаются, а сам `sh` не пробрасывает сигналы. Что это значит для остановки контейнера, разобрано в [[Сигналы в Linux#Сигналы и PID 1 в контейнере]].

```sh title="entrypoint-cmd.sh"
docker rm -f ref-ep-exec ref-ep-sh > /dev/null 2>&1
cat > both.Dockerfile <<'EOF'
FROM alpine:3.21
ENTRYPOINT ["echo", "entrypoint:"]
CMD ["default-arg"]
EOF
cat > cmd.Dockerfile <<'EOF'
FROM alpine:3.21
CMD ["echo", "cmd:", "default-arg"]
EOF
cat > shell.Dockerfile <<'EOF'
FROM alpine:3.21
ENTRYPOINT echo "shell form:" "$@"
CMD ["default-arg"]
EOF
docker build -q -f both.Dockerfile -t ref-ep:both . > /dev/null
docker build -q -f cmd.Dockerfile -t ref-ep:cmd . > /dev/null
docker build -q -f shell.Dockerfile -t ref-ep:shell . > /dev/null
echo "== ENTRYPOINT + CMD: без аргументов"
docker run --rm ref-ep:both
echo "== ENTRYPOINT + CMD: аргумент заменяет CMD"
docker run --rm ref-ep:both other-arg
echo "== только CMD: аргументы заменяют всю команду"
docker run --rm ref-ep:cmd echo replaced
echo "== --entrypoint заменяет ENTRYPOINT"
docker run --rm --entrypoint printf ref-ep:both '%s\n' from-flag
echo "== shell-форма ENTRYPOINT: CMD и аргументы не доходят"
docker run --rm ref-ep:shell other-arg
echo "== PID 1 при exec- и shell-форме ENTRYPOINT"
cat > exec.Dockerfile <<'EOF'
FROM alpine:3.21
ENTRYPOINT ["sleep", "30"]
EOF
cat > sh.Dockerfile <<'EOF'
FROM alpine:3.21
ENTRYPOINT sleep 30; echo bye
EOF
docker build -q -f exec.Dockerfile -t ref-ep:exec . > /dev/null
docker build -q -f sh.Dockerfile -t ref-ep:sh . > /dev/null
docker run -d --name ref-ep-exec ref-ep:exec > /dev/null
docker run -d --name ref-ep-sh ref-ep:sh > /dev/null
for c in ref-ep-exec ref-ep-sh; do
  printf '%s: ' "$c"
  docker exec "$c" sh -c 'tr "\0" " " < /proc/1/cmdline; echo'
done
docker rm -f ref-ep-exec ref-ep-sh > /dev/null
docker rmi ref-ep:both ref-ep:cmd ref-ep:shell ref-ep:exec ref-ep:sh > /dev/null
```

```text title="Вывод"
== ENTRYPOINT + CMD: без аргументов
entrypoint: default-arg
== ENTRYPOINT + CMD: аргумент заменяет CMD
entrypoint: other-arg
== только CMD: аргументы заменяют всю команду
replaced
== --entrypoint заменяет ENTRYPOINT
from-flag
== shell-форма ENTRYPOINT: CMD и аргументы не доходят
shell form:
== PID 1 при exec- и shell-форме ENTRYPOINT
ref-ep-exec: sleep 30 
ref-ep-sh: /bin/sh -c sleep 30; echo bye 
```

В образе `ref-ep:sh` команда `sleep 30; echo bye` нарочно составная: так `sh` остаётся процессом-оболочкой и не заменяется одной командой. В shell-форме приложение получило пустой `$@`: `other-arg` потерян. Последние две строки — PID 1: в exec-форме это сам `sleep`, в shell-форме — `/bin/sh -c …`, то есть сигнал `docker stop` придёт оболочке, а не приложению.

### Частые ошибки в ответе

- «`CMD` и `ENTRYPOINT` — одно и то же». `CMD` легко заменить аргументами `docker run`, `ENTRYPOINT` — только `--entrypoint`.
- «Shell-форма удобнее, разницы нет». Приложение не PID 1, `docker run … args` не доходят, сигналы остановки теряются. Для сервисов пишут exec-форму: `["uvicorn", "main:app"]`.
- «Аргументы `docker run` дописываются к `CMD`». Они его заменяют целиком.

## Пользователь не root

Если в образе нет `USER`, процесс работает от root (uid 0) внутри контейнера, и ошибка в приложении или в монтированном томе даёт ему больше прав, чем нужно. `USER` задаёт пользователя для следующих `RUN` и для `ENTRYPOINT` и `CMD`. Пользователя нужно создать в образе, и лучше с числовым uid. Блок показывает `id` до и после, а также то, что `--user` при запуске перекрывает `USER` образа.

```sh title="user.sh"
cat > root.Dockerfile <<'EOF'
FROM alpine:3.21
CMD ["sh", "-c", "echo uid=$(id -u) name=$(id -un)"]
EOF
cat > user.Dockerfile <<'EOF'
FROM alpine:3.21
RUN addgroup -S -g 10001 app && adduser -S -u 10001 -G app -H app
USER app
CMD ["sh", "-c", "echo uid=$(id -u) name=$(id -un)"]
EOF
docker build -q -f root.Dockerfile -t ref-user:root . > /dev/null
docker build -q -f user.Dockerfile -t ref-user:app . > /dev/null
echo "== до: без USER"
docker run --rm ref-user:root
echo "== после: USER app"
docker run --rm ref-user:app
echo "== --user переопределяет USER образа"
docker run --rm --user root ref-user:app sh -c 'echo uid=$(id -u) name=$(id -un)'
docker rmi ref-user:root ref-user:app > /dev/null
```

```text title="Вывод"
== до: без USER
uid=0 name=root
== после: USER app
uid=10001 name=app
== --user переопределяет USER образа
uid=0 name=root
```

В многоэтапной сборке `USER` ставят в итоговом этапе: образ в прод уходит из него.

### Частые ошибки в ответе

- «`USER` защитит, и дальше можно не думать». Он только снижает права процесса. Каталоги, в которые приложение пишет, нужно создать и отдать пользователю до `USER`.
- «`USER` в Dockerfile нельзя обойти». `docker run --user root` его перекрывает, так что образ сам по себе запрета не гарантирует.

## Секреты при сборке

`ARG` и `ENV` остаются в образе: по документации Docker, кто может прочитать образ, прочтёт и их. Секрет попадает не только в итоговый `ENV`: объявленный `ARG` записывается в историю слоёв, даже если ни один `RUN` его не использует: в блоке ниже `TOKEN` нигде не подставляется в команду, но попадает в строку `RUN |1 TOKEN=…`. Правильный способ — монтирование секрета на время одного `RUN`: `RUN --mount=type=secret,id=<имя>` и `docker build --secret id=<имя>,src=<файл>`; файл лежит в `/run/secrets/<имя>` только во время этого `RUN`. Значение здесь заведомо ненастоящее (`dummy-not-a-secret`), оно создаётся внутри блока.

```sh title="secrets.sh"
echo 'dummy-not-a-secret' > s.txt
cat > arg.Dockerfile <<'EOF'
FROM alpine:3.21
ARG TOKEN
ENV API_TOKEN=${TOKEN}
RUN echo "using the token" > /used.txt
EOF
cat > mount.Dockerfile <<'EOF'
FROM alpine:3.21
RUN --mount=type=secret,id=token \
    wc -c < /run/secrets/token > /token-length.txt
EOF
docker build -q -f arg.Dockerfile --build-arg TOKEN="$(cat s.txt)" -t ref-sec:arg . > /dev/null
docker build -q -f mount.Dockerfile --secret id=token,src=s.txt -t ref-sec:mount . > /dev/null
echo "== ARG и ENV: строк с секретом в docker history --no-trunc"
docker history --no-trunc --format '{{.CreatedBy}}' ref-sec:arg | grep -c dummy-not-a-secret
docker history --no-trunc --format '{{.CreatedBy}}' ref-sec:arg | grep dummy | cut -c1-90
echo "== ENV: docker image inspect"
docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' ref-sec:arg |
  grep API_TOKEN
echo "== mount=secret: совпадений в history и в inspect"
docker history --no-trunc --format '{{.CreatedBy}}' ref-sec:mount | grep -c dummy-not-a-secret
docker image inspect ref-sec:mount | grep -c dummy-not-a-secret
echo "== но RUN секрет прочитал (длина в байтах):"
docker run --rm ref-sec:mount cat /token-length.txt
docker rmi ref-sec:arg ref-sec:mount > /dev/null
```

```text title="Вывод"
== ARG и ENV: строк с секретом в docker history --no-trunc
3
RUN |1 TOKEN=dummy-not-a-secret /bin/sh -c echo "using the token" > /used.txt # buildkit
ENV API_TOKEN=dummy-not-a-secret
ARG TOKEN=dummy-not-a-secret
== ENV: docker image inspect
API_TOKEN=dummy-not-a-secret
== mount=secret: совпадений в history и в inspect
0
0
== но RUN секрет прочитал (длина в байтах):
19
```

С `ARG`/`ENV` значение видно в трёх строках истории (`ARG`, `ENV`, `RUN` с подставленным `TOKEN`) и в `Config.Env`. У образа с `--mount=type=secret` в истории и в `inspect` совпадений `0`, хотя `RUN` секрет прочитал: 19 байт — 18 символов значения и перевод строки. Образ с настоящим секретом в `ARG` уже скомпрометирован: удаление строки из Dockerfile не помогает, образ нужно пересобрать, а токен перевыпустить. Секреты времени выполнения в Kubernetes — отдельная тема: [[Конфигурация и нагрузки в Kubernetes]].

### Частые ошибки в ответе

- «ARG не попадает в образ, в отличие от ENV». Попадает: объявленный `ARG` виден в `docker history --no-trunc` в `ARG` и в последующих `RUN`, даже когда команда его не использует (проверено в блоке выше). «Не использую — значит, безопасно» неверно.
- «`unset` или `rm` в следующем `RUN` стирает секрет». Прежний слой остаётся с секретом (см. «Размер образа»).
- «Секрет можно `COPY` и удалить». Он остаётся в слое. Только `--mount=type=secret` не оставляет следов в слоях.

## Тег и digest

Тег — подвижное имя: его можно перенаправить на другой образ, и `docker pull` тем же именем скачает уже другое. Digest — хэш содержимого, по документации Docker он неизменяем; `имя@sha256:…` всегда даёт ровно тот же образ. После `docker pull` у образа есть поле `RepoDigests`. Блок скачивает закреплённую по версии `alpine:3.20.3`, запускает её по digest, а затем на `ref-`образе показывает, как перезапись тега меняет содержимое под тем же именем.

```sh title="digest.sh"
docker pull alpine:3.20.3 > /dev/null
docker image inspect --format '{{index .RepoDigests 0}}' alpine:3.20.3
echo "== запуск по digest вместо тега"
d=$(docker image inspect --format '{{index .RepoDigests 0}}' alpine:3.20.3)
docker run --rm "$d" cat /etc/alpine-release
echo "== перезапись локального тега другим образом"
mkdir t && cd t
printf 'FROM alpine:3.20.3\nRUN echo one > /v.txt\n' > Dockerfile
docker build -q -t ref-tag:1 . > /dev/null
a=$(docker image inspect --format '{{.Id}}' ref-tag:1)
printf 'FROM alpine:3.20.3\nRUN echo two > /v.txt\n' > Dockerfile
docker build -q -t ref-tag:1 . > /dev/null
b=$(docker image inspect --format '{{.Id}}' ref-tag:1)
echo "первый образ:  $a" | cut -c1-40
echo "второй образ:  $b" | cut -c1-40
[ "$a" != "$b" ] && echo "тег ref-tag:1 теперь указывает на другой образ"
docker run --rm ref-tag:1 cat /v.txt
docker image inspect "$a" > /dev/null 2>&1 || echo "первого образа по ID больше нет"
docker rmi ref-tag:1 > /dev/null
docker rmi "$a" > /dev/null 2>&1 || true
```

```text title="Вывод"
alpine@sha256:1e42bbe2508154c9126d48c2b8a75420c3544343bf86fd041fb7527e017a4b4a
== запуск по digest вместо тега
3.20.3
== перезапись локального тега другим образом
первый образ:  sha256:3fcd329826b022e1ca
второй образ:  sha256:486841f718e637307b
тег ref-tag:1 теперь указывает на другой образ
two
первого образа по ID больше нет
```

Идентификаторы образов у вас будут другими. Тег `ref-tag:1` сначала указывал на образ «one», после второй сборки — на «two»; всё, что запускается по имени `ref-tag:1`, получило другое содержимое без единого изменения в манифесте. В этой песочнице (Docker Desktop с хранилищем containerd) старый образ по ID после перезаписи уже недоступен; в другом хранилище он может остаться без имени. Базовый `alpine:3.20.3` блок оставляет, как и любой скачанный базовый образ.

Для деплоя из этого следуют два правила. В Dockerfile база закреплена по версии (`FROM python:3.13-slim`, не `latest` и не голое имя): иначе пересборка завтра даст другой образ. В манифесте Kubernetes — конкретный тег или digest; почему обновление образа под тем же тегом не запускает выкатку, показано в [[Деплой в Kubernetes]].

### Частые ошибки в ответе

- «`latest` — это самая свежая версия». Это обычное имя тега, по умолчанию `docker build -t имя` его и ставит; оно ничего не гарантирует и меняется без предупреждения, поэтому два узла могут запустить разные образы под одним именем.
- «Тег уникально определяет образ». Только на момент сборки: перезаписать его может любой, кто пушит в реестр. Неизменяем digest.
- «Закрепил базу в Dockerfile — значит, образ воспроизводим». Тег базы (даже `3.21`) тоже можно перезаписать; полностью закрепляет только digest.

## Что спросят дальше

- Почему приложение должно быть PID 1 и что будет с `SIGTERM` в shell-форме: [[Сигналы в Linux#Сигналы и PID 1 в контейнере]].
- Как выкатить новую версию образа без простоя: [[Деплой в Kubernetes]].
- Как передать конфигурацию и секреты контейнеру при запуске, а не при сборке: [[Конфигурация и нагрузки в Kubernetes]].
