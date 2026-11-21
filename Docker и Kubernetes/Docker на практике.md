---
title: Docker на практике
---

Как контейнер живёт на машине: что переживает его пересоздание (том, bind mount, `tmpfs`), как контейнеры находят друг друга по имени, что делают `EXPOSE` и `-p`, куда пишутся логи, что происходит при нехватке памяти, когда контейнер перезапускается и как Compose решает, в каком порядке стартовать сервисы. Каждая демонстрация запускает настоящие контейнеры и показывает вывод Docker. Кластер не нужен.

Как читать. Блок запускается `make k8s-block PAGE="Docker и Kubernetes/Docker на практике.md" TITLE="<имя>"`, вывод ниже снят именно так (Docker Desktop 29.5, хранилище образов containerd, cgroup v2, процессор arm64). Базовые образы закреплены по версии (`alpine:3.21`). Всё, что блок создаёт, называется с префиксом `ref-` (проект Compose — `ref-compose`) и удаляется в конце блока, в том числе при ошибке; скачанные базовые образы остаются. Адреса IP и идентификаторы у вас будут другими.

Рядом: [[Контейнеры изнутри]], [[Сигналы в Linux]], [[Образы Docker]], [[Деплой в Kubernetes]].

## Вопросы на собеседовании

1. [Что остаётся после пересоздания контейнера: данные в записываемом слое, в томе, в bind mount, в tmpfs?](#тома-bind-mount-и-tmpfs)
2. [Почему один контейнер не находит другой по имени и как это исправить?](#сети-и-имена-контейнеров)
3. [Чем отличаются EXPOSE и -p и как узнать, какой порт опубликован?](#порты-expose-и--p)
4. [Куда пишутся логи контейнера и что покажет docker logs?](#логи-контейнера)
5. [Контейнер упал с кодом 137: как понять, что это нехватка памяти, и что делать?](#лимит-памяти-и-oom)
6. [Как работает --restart и чем on-failure отличается от always?](#перезапуск-контейнера)
7. [Почему в Compose зависимый сервис стартует раньше, чем сосед готов, и как это исправить?](#compose-и-порядок-запуска)

## Тома, bind mount и tmpfs

Записываемый слой контейнера живёт, пока жив контейнер: `docker rm` удаляет его вместе с файлами (устройство слоёв — в [[Контейнеры изнутри#Слои и overlayfs]]). Данные, которые должны пережить пересоздание, кладут в смонтированное хранилище. По документации Docker, их три вида:

- том (`-v имя:/путь`) — каталог, которым управляет Docker; живёт отдельно от контейнера: `docker rm` его не трогает (явно удаляют `docker volume rm`, `docker volume prune --all`, `docker compose down -v`, а анонимный — ещё `docker rm -v`);
- bind mount (`-v /путь/на/хосте:/путь`) — каталог хоста, смонтированный как есть;
- `tmpfs` (`--tmpfs /путь`) — память хоста; при остановке контейнера содержимое пропадает.

Блок пишет по файлу в каждое место и в обычный каталог `/layer`, удаляет контейнер и запускает новый с теми же монтированиями. Первый контейнер показывает типы монтирований через `docker inspect`.

```sh title="storage.sh"
docker rm -f ref-st1 ref-st2 > /dev/null 2>&1
docker volume rm ref-vol > /dev/null 2>&1
cleanup() {
  docker rm -f ref-st1 ref-st2 > /dev/null 2>&1
  docker volume rm ref-vol > /dev/null 2>&1
}
trap cleanup EXIT
mkdir host
docker volume create ref-vol > /dev/null
mounts="-v ref-vol:/vol -v $PWD/host:/bind --tmpfs /mem"
docker run --name ref-st1 $mounts alpine:3.21 sh -c '
  for d in /vol /bind /mem /layer; do mkdir -p $d; echo data > $d/f.txt; done'
echo "== монтирования первого контейнера"
docker inspect ref-st1 -f \
  'Mounts: {{range $i, $m := .Mounts}}{{if $i}}, {{end}}{{$m.Type}} {{$m.Destination}}{{end}}'
docker inspect -f 'tmpfs (HostConfig.Tmpfs): {{.HostConfig.Tmpfs}}' ref-st1
docker rm ref-st1 > /dev/null
echo "== новый контейнер с теми же монтированиями: есть ли файл"
docker run --name ref-st2 $mounts alpine:3.21 sh -c '
  for d in /vol /bind /mem /layer; do
    if [ -e $d/f.txt ]; then echo "$d: есть"; else echo "$d: нет"; fi
  done'
echo "== файл bind mount на хосте"
ls host
```

```text title="Вывод"
== монтирования первого контейнера
Mounts: volume /vol, bind /bind
tmpfs (HostConfig.Tmpfs): map[/mem:]
== новый контейнер с теми же монтированиями: есть ли файл
/vol: есть
/bind: есть
/mem: нет
/layer: нет
== файл bind mount на хосте
f.txt
```

Читать так. Том и bind mount пережили удаление контейнера: файл на месте. В `tmpfs` и в обычном каталоге `/layer` его нет: `tmpfs` живёт в памяти, а `/layer` лежал в записываемом слое первого контейнера, который удалил `docker rm`. Каталог `host` на хосте сохранил файл, потому что bind mount — это сам каталог хоста. В выводе `inspect` `tmpfs` не входит в список `Mounts`: он записан отдельно, в `HostConfig.Tmpfs`.

### Частые ошибки в ответе

- «Данные в контейнере сохранятся, пока образ не удалён». Записываемый слой принадлежит контейнеру, а не образу: `docker rm` его стирает. Состояние (база, загрузки) выносят в том.
- «`docker rm` удалит и том». Именованный том остаётся, пока его не удалят отдельно; поэтому остановленная база не теряет данные, а `docker volume ls` со временем копит забытые тома.
- «`docker system prune` — безобидная уборка». Он удаляет не ваши объекты, а всё неиспользуемое на машине: остановленные контейнеры, сети без контейнеров, образы без тега (dangling) и неиспользуемый кэш сборки; `--volumes` добавляет неиспользуемые анонимные тома (по документации Docker 29; именованные удаляет отдельная команда `docker volume prune --all`, то есть данные остановленных баз). Образы без контейнеров целиком чистит `docker image prune -a`. Перед запуском смотрят, что попадёт под удаление, а на рабочей машине предпочитают точечные `docker rm`, `docker rmi`, `docker volume rm`.
- «tmpfs подходит для данных, которые нельзя потерять». Он целиком в памяти и пропадает при остановке; его место — временные файлы: по документации Docker, он хранится только в памяти хоста и удаляется при остановке контейнера.

## Сети и имена контейнеров

В сети `bridge`, которую Docker создаёт по умолчанию, контейнеры видят друг друга по IP, но имена не резолвятся. Автоматический DNS по имени контейнера включён в пользовательских сетях (`docker network create`). У проекта Compose есть своя сеть по умолчанию, и в блоке ниже сервис находит соседа по имени.

Блок запускает контейнер `ref-a` в сети по умолчанию и пробует найти его по имени из второго контейнера, затем повторяет то же в сети `ref-net`.

```sh title="network.sh"
docker rm -f ref-a ref-b > /dev/null 2>&1
docker network rm ref-net > /dev/null 2>&1
cleanup() {
  docker rm -f ref-a ref-b > /dev/null 2>&1
  docker network rm ref-net > /dev/null 2>&1
}
trap cleanup EXIT
docker run -d --name ref-a alpine:3.21 sleep 120 > /dev/null
echo "== сеть по умолчанию: ping по имени"
docker run --rm alpine:3.21 ping -c 1 -W 2 ref-a 2>&1 | head -1
docker rm -f ref-a > /dev/null
docker network create ref-net > /dev/null
docker run -d --name ref-b --network ref-net alpine:3.21 sleep 120 > /dev/null
echo "== сеть ref-net: ping по имени"
docker run --rm --network ref-net alpine:3.21 ping -c 1 -W 2 ref-b 2>&1 | sed -n '1,2p'
```

```text title="Вывод"
== сеть по умолчанию: ping по имени
ping: bad address 'ref-a'
== сеть ref-net: ping по имени
PING ref-b (172.25.0.2): 56 data bytes
64 bytes from 172.25.0.2: seq=0 ttl=64 time=0.047 ms
```

В сети по умолчанию имя `ref-a` не резолвится: `ping: bad address`. В пользовательской сети `ref-net` имя `ref-b` превратилось в адрес. IP-адрес и время ответа у вас будут другими.

### Частые ошибки в ответе

- «В Docker контейнеры всегда находят друг друга по имени». Только в пользовательских сетях; в `bridge` по умолчанию имена не резолвятся, и приложение получает `bad address`.
- «Адрес соседа можно прописать IP-адресом». Адрес не закреплён: по документации Docker, остановленный контейнер теряет IP; по имени в пользовательской сети надёжнее.
- «`localhost` внутри контейнера — это хост». У контейнера свой сетевой namespace, `localhost` — он сам (см. [[Контейнеры изнутри#Namespaces и unshare]]); к соседу обращаются по имени сервиса.

## Порты, EXPOSE и -p

`EXPOSE` — метаданные образа: документация Docker называет его способом задокументировать, какой порт слушает приложение, и он ничего не публикует. Порт становится доступен с хоста только при публикации: `-p хост:контейнер` или `-P`, который публикует все `EXPOSE`-порты на случайные порты хоста. Фраза «`EXPOSE` открывает порт» неверна: он только документирует намерение.

Блок собирает образ с `EXPOSE 8080` и простым HTTP-сервером busybox и запускает его дважды. Во втором запуске порт публикуется на `127.0.0.1` со случайным портом хоста (`-p 127.0.0.1::8080`), чтобы не занять чужой порт, и `docker port` показывает соответствие.

```sh title="ports.sh"
docker rm -f ref-web1 ref-web2 > /dev/null 2>&1
cleanup() {
  docker rm -f ref-web1 ref-web2 > /dev/null 2>&1
  docker rmi ref-web:1 > /dev/null 2>&1
}
trap cleanup EXIT
cat > reply.sh <<'EOF'
#!/bin/sh
printf 'HTTP/1.0 200 OK\r\n\r\nhello-from-container\n'
EOF
cat > Dockerfile <<'EOF'
FROM alpine:3.21
COPY --chmod=755 reply.sh /reply.sh
EXPOSE 8080
CMD ["nc", "-lk", "-p", "8080", "-e", "/reply.sh"]
EOF
docker build -q -t ref-web:1 . > /dev/null
docker run -d --name ref-web1 ref-web:1 > /dev/null
echo "== без -p: колонка PORTS, docker port и запросы с хоста"
docker ps --filter name=ref-web1 --format '{{.Names}}  {{.Ports}}'
echo "docker port: [$(docker port ref-web1)]"
if nc -z 127.0.0.1 8080 2> /dev/null; then
  echo "порт 8080 на хосте занят, проверка неоднозначна" >&2; exit 1
fi
curl -s --max-time 3 http://127.0.0.1:8080/ > /dev/null
echo "curl 127.0.0.1:8080 (без -p): код выхода $?"
docker run -d --name ref-web2 -p 127.0.0.1::8080 ref-web:1 > /dev/null
echo "== с -p 127.0.0.1::8080"
docker ps --filter name=ref-web2 --format '{{.Names}}  {{.Ports}}' |
  sed 's/:[0-9]*->/:PORT->/'
docker port ref-web2 | sed 's/:[0-9]*$/:PORT/'
hostport=$(docker port ref-web2 8080/tcp | sed 's/.*://')
ok=0
for _ in $(seq 20); do
  if curl -fs "http://127.0.0.1:$hostport/"; then ok=1; break; fi
  sleep 0.5
done
[ "$ok" = 1 ] || { echo "сервер не ответил за 10 с" >&2; exit 1; }
```

```text title="Вывод"
== без -p: колонка PORTS, docker port и запросы с хоста
ref-web1  8080/tcp
docker port: []
curl 127.0.0.1:8080 (без -p): код выхода 7
== с -p 127.0.0.1::8080
ref-web2  127.0.0.1:PORT->8080/tcp
8080/tcp -> 127.0.0.1:PORT
hello-from-container
```

Без `-p` колонка `PORTS` показывает только `8080/tcp` (порт из `EXPOSE`), а `docker port` ничего не печатает: опубликованных портов нет. Запрос с хоста на `127.0.0.1:8080` завершился с кодом 7 (`curl` не смог подключиться): порт 8080 с хоста недоступен. Перед запросом блок проверяет, что порт 8080 на хосте свободен, иначе ответ чужого сервера исказил бы проверку. С `-p 127.0.0.1::8080` Docker выбрал случайный порт хоста (в выводе он заменён на `PORT`, у вас будет свой), и запрос к нему вернул ответ контейнера.

### Частые ошибки в ответе

- «`EXPOSE` публикует порт». Нет: без `-p` или `-P` порт с хоста недоступен: `docker port` пуст, а запрос с хоста не проходит (код 7 в блоке).
- «`-p 8080:8080` безопасно, это же локальная разработка». Без адреса порт публикуется на все интерфейсы хоста; для локального сервиса пишут `-p 127.0.0.1:8080:8080`.

## Логи контейнера

`docker logs` читает то, что процесс контейнера пишет в stdout и stderr: Docker забирает оба потока и отдаёт их драйвером журнала, по умолчанию `json-file`. Файл, в который приложение пишет само, `docker logs` не увидит. Отсюда правило для контейнеров: логи — в stdout, а не в файл. Документация Kubernetes называет запись в стандартный вывод и поток ошибок самым распространённым способом логирования контейнерных приложений.

Блок запускает контейнер, который пишет четыре строки в stdout и одну в stderr, и читает журнал по потокам.

```sh title="logs.sh"
docker rm -f ref-log > /dev/null 2>&1
trap 'docker rm -f ref-log > /dev/null 2>&1' EXIT
docker run -d --name ref-log alpine:3.21 sh -c '
  echo "app: старт"; echo "app: запрос 1"; echo "app: запрос 2"
  echo "app: ошибка" >&2
  echo "app: дальше записей не будет"; sleep 120' > /dev/null
ok=0
for _ in $(seq 20); do
  docker logs ref-log 2>&1 | grep -q "дальше записей" && { ok=1; break; }
  sleep 0.5
done
[ "$ok" = 1 ] || { echo "логов нет за 10 с" >&2; exit 1; }
echo "== stdout контейнера (stderr отброшен)"
docker logs ref-log 2> /dev/null
echo "== stderr контейнера (stdout отброшен)"
docker logs ref-log 2>&1 > /dev/null
echo "== драйвер журнала"
docker inspect -f '{{.HostConfig.LogConfig.Type}}' ref-log
```

```text title="Вывод"
== stdout контейнера (stderr отброшен)
app: старт
app: запрос 1
app: запрос 2
app: дальше записей не будет
== stderr контейнера (stdout отброшен)
app: ошибка
== драйвер журнала
json-file
```

Docker хранит потоки раздельно, поэтому их читают по отдельности; при `2>&1` порядок строк между stdout и stderr не гарантируется (в одном из пробных прогонов строка stderr вышла раньше строк stdout, хотя была записана позже). `docker logs` не знает о файлах, которые приложение создаёт внутри контейнера.

### Частые ошибки в ответе

- «Логи приложения надо писать в файл внутри контейнера». Тогда `docker logs` их не покажет (так сказано и в документации Docker), а файл растёт в записываемом слое и пропадает вместе с контейнером. Пишут в stdout, а сбор и хранение — задача платформы.
- «`docker logs` показывает только stdout». Показывает оба потока, и stderr можно отделить, как в блоке; но порядок строк между потоками при `2>&1` не гарантируется, поэтому приложению лучше писать структурированные записи с временем.

## Лимит памяти и OOM

`--memory` задаёт предел памяти в cgroup контейнера. Когда процесс его превышает и ядро не может освободить страницы, оно убивает процесс. Как cgroup v2 доходит до убийства, разобрано в [[Контейнеры изнутри#cgroups v2 и память]], а откуда код 137 — в [[Сигналы в Linux#Коды выхода 128 + N]]; здесь важно, как это увидеть снаружи: в `docker inspect` у такого контейнера `State.OOMKilled` равен `true`.

Блок запускает контейнер с `--memory 64m`, процесс которого читает бесконечный поток без переводов строки: `tail /dev/zero` копит всё в памяти. `--memory-swap` равен `--memory`, иначе процесс мог бы уйти в подкачку. Второй контейнер без нехватки памяти (`true`) нужен для сравнения.

```sh title="oom.sh"
docker rm -f ref-oom ref-ok > /dev/null 2>&1
trap 'docker rm -f ref-oom ref-ok > /dev/null 2>&1' EXIT
docker run -d --name ref-oom --memory 64m --memory-swap 64m alpine:3.21 \
  tail /dev/zero > /dev/null
docker run -d --name ref-ok --memory 64m --memory-swap 64m alpine:3.21 \
  true > /dev/null
for c in ref-oom ref-ok; do
  ok=0
  for _ in $(seq 60); do
    [ "$(docker inspect -f '{{.State.Status}}' $c)" = exited ] && { ok=1; break; }
    sleep 0.5
  done
  [ "$ok" = 1 ] || { echo "$c не завершился за 30 с" >&2; exit 1; }
done
echo "лимит памяти: $(docker inspect -f '{{.HostConfig.Memory}}' ref-oom) байт"
echo "OOMKilled ExitCode:"
for c in ref-oom ref-ok; do
  echo "  $c: $(docker inspect -f '{{.State.OOMKilled}} {{.State.ExitCode}}' $c)"
done
```

```text title="Вывод"
лимит памяти: 67108864 байт
OOMKilled ExitCode:
  ref-oom: true 137
  ref-ok: false 0
```

`ref-oom` убит ядром: `OOMKilled=true`, код 137. У `ref-ok` лимит тот же, но память не кончилась: `false 0`. Поэтому при разборе падения смотрят оба поля сразу: если `OOMKilled` равен `false`, а код 137, процесс убил кто-то другой (`docker kill`, `docker stop` по таймауту). Память этой песочницы — память ВМ Docker Desktop, а не Mac.

### Частые ошибки в ответе

- «Контейнер падает от нехватки памяти — уберу лимит». Это прячет утечку и переносит риск на всю машину: без лимита процесс забирает память хоста, и жертву выбирает OOM-killer хоста, не обязательно прожорливый процесс. Сначала смотрят, почему растёт память, и только потом решают, нужен ли лимит больше.
- «Код 137 — всегда OOM». 137 — это любой `KILL` (в том числе `docker kill` и `docker stop` по истечении таймаута); о нехватке памяти говорит флаг `OOMKilled` в `docker inspect`.
- «`--memory` ограничивает только heap приложения». Лимит действует на cgroup контейнера целиком, то есть на все его процессы и всё, что ядро учитывает в этой cgroup (см. [[Контейнеры изнутри#cgroups v2 и память]]).

## Перезапуск контейнера

`--restart` — политика, по которой демон Docker запускает контейнер снова после выхода процесса. По документации Docker: `no` (по умолчанию) не перезапускает; `on-failure[:N]` — только при ненулевом коде выхода и не более N раз; `always` — перезапускает при любой остановке, а вручную остановленный контейнер поднимается снова только при перезапуске демона; `unless-stopped` похож на `always`, но вручную остановленный не поднимается и после перезапуска демона. Перед каждым перезапуском добавляется пауза: она удваивается, начиная со 100 мс, до предела в одну минуту.

Блок запускает два контейнера с `--restart on-failure:3`: процесс одного завершается с кодом 1, другого — с кодом 0. Счётчик попыток — `RestartCount` в `docker inspect`.

```sh title="restart.sh"
docker rm -f ref-fail ref-fine > /dev/null 2>&1
trap 'docker rm -f ref-fail ref-fine > /dev/null 2>&1' EXIT
docker run -d --name ref-fail --restart on-failure:3 alpine:3.21 \
  sh -c 'exit 1' > /dev/null
docker run -d --name ref-fine --restart on-failure:3 alpine:3.21 \
  sh -c 'exit 0' > /dev/null
ok=0
for _ in $(seq 60); do
  n=$(docker inspect -f '{{.RestartCount}}' ref-fail)
  s=$(docker inspect -f '{{.State.Status}}' ref-fail)
  [ "$n" = 3 ] && [ "$s" = exited ] && { ok=1; break; }
  sleep 0.5
done
[ "$ok" = 1 ] || { echo "ref-fail не дошёл до RestartCount 3 за 30 с" >&2; exit 1; }
echo "container  restarts  status  exit"
for c in ref-fail ref-fine; do
  fmt='{{.RestartCount}} {{.State.Status}} {{.State.ExitCode}}'
  echo "$c $(docker inspect -f "$fmt" $c)"
done
```

```text title="Вывод"
container  restarts  status  exit
ref-fail 3 exited 1
ref-fine 0 exited 0
```

У `ref-fail` счётчик дошёл до 3 и остановился: после исходной попытки Docker перезапустил контейнер три раза и перестал. У `ref-fine` процесс вышел с кодом 0, перезапусков нет.

### Частые ошибки в ответе

- «`--restart always` лечит падающее приложение». Он только перезапускает: приложение с ошибкой при старте будет падать по кругу, а причина останется в `docker logs`. На ненулевой код выхода без повторов стоит смотреть логи, а не наращивать число попыток.
- «`on-failure` перезапускает и при успешном завершении». Нет: при коде 0 перезапуска нет, как у `ref-fine`.
- «Docker `--restart` и Kubernetes `restartPolicy` — одно и то же». Это разные механизмы разных систем; как выглядят повторные падения пода (`CrashLoopBackOff`), см. [[Отладка в Kubernetes]].

## Compose и порядок запуска

Compose описывает набор сервисов в одном файле и запускает их как проект: свои сети, тома и имена `<проект>-<сервис>-<номер>`. `depends_on` без условия ждёт только запуска контейнера соседа: по документации Compose, он не ждёт, пока контейнер «готов», только пока он запущен. Если сервису нужна готовность соседа, добавляют `healthcheck` соседу и `condition: service_healthy` в `depends_on`.

Блок запускает один и тот же проект `ref-compose` дважды. Сервис `db` пять секунд «разогревается» и только потом начинает слушать порт 5432, сервис `app` один раз проверяет `nc -z db 5432` и выходит. В первом файле зависимость без условия, во втором — с проверкой здоровья и `service_healthy`. В конце каждого прогона — `docker compose down -v`, который удаляет контейнеры, сеть проекта и тома.

```sh title="compose.sh"
dc() { f=$1; shift; docker compose -p ref-compose -f "$f" "$@"; }
cleanup() {
  for f in plain.yml healthy.yml; do
    [ -f "$f" ] && dc "$f" down -v --remove-orphans > /dev/null 2>&1
  done
  docker rm -f ref-compose-app-1 ref-compose-db-1 > /dev/null 2>&1
  docker network rm ref-compose_default > /dev/null 2>&1
}
cat > plain.yml <<'EOF'
services:
  db:
    image: alpine:3.21
    command: sh -c 'sleep 5; nc -lk -p 5432 -e true'
  app:
    image: alpine:3.21
    depends_on:
      - db
    command:
      - sh
      - -c
      - nc -z -w 1 db 5432 && echo "db готов" || echo "db не отвечает"
EOF
cat > healthy.yml <<'EOF'
services:
  db:
    image: alpine:3.21
    command: sh -c 'sleep 5; nc -lk -p 5432 -e true'
    healthcheck:
      test: ["CMD-SHELL", "nc -z localhost 5432"]
      interval: 1s
      timeout: 1s
      retries: 30
  app:
    image: alpine:3.21
    depends_on:
      db:
        condition: service_healthy
    command:
      - sh
      - -c
      - nc -z -w 1 db 5432 && echo "db готов" || echo "db не отвечает"
EOF
cleanup
trap cleanup EXIT
for f in plain.yml healthy.yml; do
  echo "== $f"
  dc "$f" up -d > /dev/null 2>&1 || { echo "up не удался" >&2; exit 1; }
  ok=0
  for _ in $(seq 60); do
    st=$(docker inspect -f '{{.State.Status}}' ref-compose-app-1)
    [ "$st" = exited ] && { ok=1; break; }
    sleep 0.5
  done
  [ "$ok" = 1 ] || { echo "app не завершился за 30 с" >&2; exit 1; }
  dc "$f" logs --no-log-prefix app
  dc "$f" down -v > /dev/null 2>&1
done
```

```text title="Вывод"
== plain.yml
db не отвечает
== healthy.yml
db готов
```

В первом файле `app` стартовал, как только контейнер `db` запустился, спросил порт, пока `db` ещё «разогревался», и получил отказ. Во втором `docker compose up -d` дождался состояния `healthy` у `db` и только потом запустил `app`, который подключился. Разница не в самом `depends_on`, а в `condition: service_healthy`.

### Частые ошибки в ответе

- «`depends_on` ждёт, пока сосед будет готов». Без `condition` он ждёт только запуска контейнера; готовность процесса проверяет `healthcheck` с `service_healthy`.
- «Достаточно `restart: always` у приложения, оно дождётся базы». Это скрывает проблему падениями и перезапусками; надёжнее ожидание готовности и повторы подключения в самом приложении.
- «`docker compose down` удаляет и данные». Без `-v` именованные тома остаются; с `-v` удаляются тома проекта вместе с данными, поэтому на рабочей базе `-v` не пишут.

## Что спросят дальше

- Как устроены слои и почему данные из записываемого слоя исчезают: [[Контейнеры изнутри#Слои и overlayfs]] и [[Образы Docker]].
- Как cgroup доводит процесс до `OOMKilled` и что такое 137: [[Контейнеры изнутри#cgroups v2 и память]], [[Сигналы в Linux#Коды выхода 128 + N]].
- Чем пробы Kubernetes похожи на `healthcheck` и как перезапуск устроен в Kubernetes: [[Деплой в Kubernetes]], [[Отладка в Kubernetes]].
