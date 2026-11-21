---
title: Отладка в Kubernetes
---

Что делать, если под не стартует, падает или не получает трафик: общий порядок команд на чтение и пять сценариев, каждый вызван в локальном кластере kind. По выводу видно признак, по которому сценарий узнают: `CrashLoopBackOff`, `OOMKilled`, `Pending`, `ImagePullBackOff` и под `Running`, но не `Ready`. Продолжение страниц «Деплой в Kubernetes» и «Ресурсы и остановка в Kubernetes»; Service без ответа, 502 при выкатке, троттлинг CPU и способы зайти в под разобраны на странице [[Отладка сервисов и нагрузки в Kubernetes]].

Как читать. Поднять песочницу: `make k8s-up`; блок запускается `make k8s-block PAGE="Docker и Kubernetes/Отладка в Kubernetes.md" TITLE="<имя>"`, вывод ниже снят именно так. В песочнице один узел kind (Kubernetes v1.37.0), образы `reference-app:1` и `busybox:1.37.0` загружены в узел без реестра, поэтому в манифестах `imagePullPolicy: IfNotPresent`. Демо-приложение ломается по переменным окружения: `CRASH_ON_START=1` — выход с кодом 1 при старте, `ALLOC_MB=200` — выделить и занять 200 МиБ, `NOT_READY=1` — `/ready` отвечает 503. Каждый сценарий работает в своём namespace `demo-<слаг>`: его блок создаёт заново и в конце удаляет. Имена подов, возраст и счётчики перезапусков у вас будут другими. Строки длиннее ~90 символов обрезаны, обрезанное помечено `…`.

Рядом: [[Деплой в Kubernetes]], [[Ресурсы и остановка в Kubernetes]], [[Объекты Kubernetes]], [[Контейнеры изнутри]], [[Сигналы в Linux]].

## Вопросы на собеседовании

1. [С чего начать, если под не работает?](#общий-порядок)
2. [Под в CrashLoopBackOff: что смотреть и откуда видно причину?](#crashloopbackoff)
3. [Контейнер перезапускается с кодом 137: что это и чем отличается от Evicted?](#oomkilled)
4. [Под висит в Pending: почему?](#pending)
5. [ImagePullBackOff: что проверять?](#imagepullbackoff)
6. [Под Running, но READY 0/1: что происходит и пойдёт ли на него трафик?](#под-running-но-не-ready)

## Общий порядок

Порядок один для любого сценария, и все его команды только читают:

1. `kubectl get pods` — статус, `READY`, `RESTARTS`: что вообще происходит.
2. `kubectl describe pod <под>` — внизу блок `Events`, посередине `State` и `Last State` с причиной и кодом выхода.
3. `kubectl logs <под>` и `kubectl logs <под> --previous` — что писало приложение; `--previous` показывает предыдущий экземпляр контейнера, если он был (по документации kubectl).
4. `kubectl get events --sort-by=.lastTimestamp` — события namespace по времени: что происходило до и вокруг пода.

Пример, не прогоняется:

```sh
kubectl get pods -n <ns>
kubectl describe pod <под> -n <ns>
kubectl logs <под> -n <ns> --previous
kubectl get events -n <ns> --sort-by=.lastTimestamp
```

Кроме `kubectl logs` в списке нет ничего про приложение: до его логов дело доходит, только если контейнер вообще стартовал. Если под не запланирован или образ не скачан, причина лежит в событиях, а логов нет. Поэтому смотрят сверху вниз: статус, события, и только потом логи.

### Частые ошибки в ответе

- «Под глючит, удалю его, пересоздастся». `kubectl delete pod` у пода из Deployment создаст такой же под из того же шаблона, и причина сработает снова; к тому же вместе с подом пропадают события и состояние, по которым причину и ищут. Сначала описание и логи.
- «`kubectl logs` — и всё видно». Без `--previous` читается текущий экземпляр, а причина падения лежит в логе предыдущего. В прогоне ниже читается именно `--previous`.
- «Поправлю прямо на проде через `kubectl edit`». Живой объект расходится с манифестом в репозитории: следующий деплой молча вернёт старое, а правка осталась без ревью и истории. Исправление идёт тем же путём, что и любое изменение.
- «`kubectl delete pod --force --grace-period=0` — быстрый способ разблокировать». Принудительное удаление не ждёт подтверждения kubelet, что под остановлен, и сразу освобождает имя; по документации Kubernetes, у StatefulSet это ведёт к двум одновременно работающим подам с одной идентичностью и потере данных. Сначала выясняют, почему под не завершается.

## CrashLoopBackOff

Контейнер стартует и сразу падает; kubelet перезапускает его (по умолчанию политика `restartPolicy: Always`) с растущей задержкой. Состояние `CrashLoopBackOff` как раз означает, что задержка перед перезапуском сейчас действует, а не что с контейнером что-то случилось само: документация описывает его как работу экспоненциальной задержки перезапусков, которая сбрасывается, если контейнер проработал достаточно долго (в документации пример — 10 минут).

Опыт: под с `CRASH_ON_START=1`. Блок ждёт `RESTARTS` не меньше 2 при статусе `Error` (предел 60 секунд), снимает `get` и `describe`, затем ждёт, пока `logs --previous` вернёт лог (предел 260 секунд), и читает события. Оба ожидания с выходом по таймауту. Почему не ждать статуса `CrashLoopBackOff`, объяснено под выводом.

```sh title="crashloop.sh"
kubectl delete namespace demo-crash --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-crash
kubectl apply -n demo-crash -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: crasher
spec:
  containers:
    - name: app
      image: reference-app:1
      imagePullPolicy: IfNotPresent
      env:
        - {name: CRASH_ON_START, value: "1"}
YAML
short() { awk '{ if (length($0) > 90) print substr($0, 1, 89) "…"; else print }'; }
ok=""
for _ in $(seq 60); do
  read -r st rs < <(kubectl get pod crasher -n demo-crash --no-headers | awk '{ print $3, $4 }')
  [ "$st" = Error ] && [ "${rs:-0}" -ge 2 ] 2>/dev/null && { ok=1; break; }
  sleep 1
done
[ -n "$ok" ] || { echo "timeout: no RESTARTS >= 2 in status Error" >&2; exit 1; }
echo "--- 1. get pods"
kubectl get pods -n demo-crash
echo "--- 2. describe: Last State"
kubectl describe pod crasher -n demo-crash | grep -A3 'Last State'
ok=""
for _ in $(seq 130); do
  log="$(kubectl logs crasher -n demo-crash --previous 2>/dev/null)" &&
    [ -n "$log" ] && [ "${log#unable to retrieve}" = "$log" ] && { ok=1; break; }
  sleep 2
done
[ -n "$ok" ] || { echo "timeout: no previous log" >&2; exit 1; }
echo "--- 3. logs --previous"
echo "$log"
echo "--- 4. events"
kubectl get events -n demo-crash --sort-by=.lastTimestamp \
  -o custom-columns='TYPE:.type,REASON:.reason,MESSAGE:.message' | short
kubectl delete namespace demo-crash --wait --timeout=120s
```

```text title="Вывод"
namespace/demo-crash created
pod/crasher created
--- 1. get pods
NAME      READY   STATUS   RESTARTS      AGE
crasher   0/1     Error    2 (14s ago)   15s
--- 2. describe: Last State
    Last State:     Terminated
      Reason:       Error
      Exit Code:    1
      Started:      Sat, 21 Nov 2026 16:01:42 +0300
--- 3. logs --previous
fatal: CRASH_ON_START=1
--- 4. events
TYPE      REASON      MESSAGE
Normal    Scheduled   Successfully assigned demo-crash/crasher to reference-control-plane
Normal    Pulled      Container image "reference-app:1" already present on machine and ca…
Normal    Created     Container created
Normal    Started     Container started
Warning   BackOff     Back-off restarting failed container app in pod crasher_demo-crash(…
namespace "demo-crash" deleted
```

Признаки сценария: `RESTARTS` растёт (в скобках — как давно был последний перезапуск), `Last State: Terminated` с кодом выхода 1 (приложение само вышло с ошибкой, `Reason: Error`), в событиях `Warning BackOff` с текстом `Back-off restarting failed container`, а причину называет `logs --previous`: `fatal: CRASH_ON_START=1`. Статус в колонке `STATUS` документация называет `CrashLoopBackOff`; в песочнице (Kubernetes v1.37.0) он наблюдался иначе: пока контейнер упавший между попытками, `STATUS` показывает `Error` (так и в выводе выше), а `CrashLoopBackOff` в отдельном наблюдении появился только через четыре минуты, после пяти перезапусков (интервалы между перезапусками там были около 13, 23, 46, 83 секунд). Поэтому узнают сценарий не по одному слову в `STATUS`, а по росту `RESTARTS`, `Last State` и событию `BackOff`. Похожая особенность — с `--previous`: в тех же наблюдениях он больше полутора минут отвечал `unable to retrieve container logs`, пока текущий лог читался обычным `kubectl logs`, поэтому блок повторяет попытки. На своём кластере поведение может отличаться. События `Pulled`, `Created`, `Started` на каждой попытке повторяются: одинаковые события сворачиваются в одно со счётчиком `count`.

Причина лежит в приложении или его настройке: ошибка конфигурации, недоступная зависимость при старте, неверная команда. Лечение — исправить причину и выкатить заново, а не перезапускать под.

### Частые ошибки в ответе

- «`CrashLoopBackOff` — это ошибка Kubernetes». Это состояние ожидания между перезапусками; ошибка в приложении, и Kubernetes её лишь показывает.
- «Подожду, перезапустится и заработает». Без изменения причины каждая попытка кончится так же, а задержка между попытками растёт.
- «`kubectl logs` покажет причину». Без `--previous` читается текущий экземпляр контейнера, а причина падения — в логе прошлого; документация советует `--previous` для контейнера, который уже падал.

## OOMKilled

Если контейнер занял памяти больше `limits.memory`, ядро убивает его процесс по `KILL`, и `Last State` показывает `Reason: OOMKilled` с кодом выхода 137. Как cgroup v2 доводит дело до убийства, показано в [[Контейнеры изнутри#cgroups v2 и память]], а почему 137 — это 128 + 9 (`KILL`), в [[Сигналы в Linux#Коды выхода 128 + N]]. Здесь важно, как это выглядит из Kubernetes и чем отличается от `Evicted`.

Опыт: под с `ALLOC_MB=200` и лимитом памяти 100 МиБ. Приложение занимает память страницами, так что на старте оно ещё до первой строки лога упирается в лимит. Блок ждёт `OOMKilled` в `Last State`.

```sh title="oom.sh"
kubectl delete namespace demo-oom --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-oom
kubectl apply -n demo-oom -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: hungry
spec:
  containers:
    - name: app
      image: reference-app:1
      imagePullPolicy: IfNotPresent
      env:
        - {name: ALLOC_MB, value: "200"}
      resources:
        requests: {memory: 100Mi}
        limits: {memory: 100Mi}
YAML
ok=""
for _ in $(seq 60); do
  r="$(kubectl get pod hungry -n demo-oom -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}')"
  [ "$r" = OOMKilled ] && { ok=1; break; }
  sleep 2
done
[ -n "$ok" ] || { echo "timeout: no OOMKilled in Last State" >&2; exit 1; }
echo "--- describe: Last State"
kubectl describe pod hungry -n demo-oom | grep -A3 'Last State'
echo "--- limit"
kubectl get pod hungry -n demo-oom -o jsonpath='{.spec.containers[0].resources.limits}{"\n"}'
kubectl delete namespace demo-oom --wait --timeout=120s
```

```text title="Вывод"
namespace/demo-oom created
pod/hungry created
--- describe: Last State
    Last State:     Terminated
      Reason:       OOMKilled
      Exit Code:    137
      Started:      Sat, 21 Nov 2026 16:04:34 +0300
--- limit
{"memory":"100Mi"}
namespace "demo-oom" deleted
```

Отличие от `Evicted` в том, кто и как убивает. `OOMKilled` — это сработавший лимит контейнера: убивает ядро один контейнер, а kubelet перезапускает его на том же узле тем же подом (по документации Kubernetes, контейнер, убитый за память, перезапускается). `Evicted` — вытеснение под давлением на узел: kubelet при нехватке ресурсов узла (память, диск) сам выбирает поды, переводит их в фазу `Failed` и завершает целиком; пересоздаёт такой под уже контроллер (Deployment, StatefulSet). Поэтому `OOMKilled` лечат в приложении или в его лимите, а вытеснение — в `requests` (под без них вытесняют первым, см. [[Ресурсы и остановка в Kubernetes#Requests и limits]]) и в ёмкости узлов.

Что делать: посмотреть, сколько приложению нужно на самом деле (утечка, размер кучи, размер пачки данных), и только потом поднимать лимит. На странице вывода нет лога: убитый процесс не успел ничего написать, поэтому `logs --previous` тут не поможет, причина читается из `Last State`.

### Частые ошибки в ответе

- «Поставим лимит памяти побольше или уберём совсем». Без лимита процесс с утечкой съест память узла, и вытеснять начнут соседей; лимит поднимают, когда понятно, сколько приложению нужно, а утечку чинят в коде.
- «`OOMKilled` и `Evicted` — одно и то же». Первое — лимит контейнера, ядро и тот же под; второе — давление на узел, решение kubelet и новый под.
- «Код 137 всегда значит OOM». Это `KILL`; такой же код дают и истёкший `terminationGracePeriodSeconds`, и ручной `kill -9`; причину называет `Reason` в `Last State`.

## Pending

Под в `Pending` не запланирован ни на один узел или ещё не запустился. По документации Kubernetes, чаще всего причина в том, что ресурсов какого-то вида не хватает, и сообщения планировщика лежат в событиях `describe`.

Опыт: под, которому нужно 64 ГиБ памяти (`requests.memory`) при памяти узла около 8 ГиБ. Лимиты не заданы: планировщик смотрит на `requests`.

```sh title="pending.sh"
kubectl delete namespace demo-pending --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-pending
kubectl apply -n demo-pending -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: big
spec:
  containers:
    - name: app
      image: reference-app:1
      imagePullPolicy: IfNotPresent
      resources:
        requests: {memory: 64Gi}
YAML
short() { awk '{ if (length($0) > 90) print substr($0, 1, 89) "…"; else print }'; }
ok=""
for _ in $(seq 30); do
  kubectl get events -n demo-pending --field-selector reason=FailedScheduling -o name | grep -q . && { ok=1; break; }
  sleep 1
done
[ -n "$ok" ] || { echo "timeout: no FailedScheduling event" >&2; exit 1; }
echo "--- get pods"
kubectl get pods -n demo-pending
echo "--- events"
kubectl get events -n demo-pending -o custom-columns='REASON:.reason,MESSAGE:.message' | short
echo "--- node allocatable memory"
kubectl get nodes -o custom-columns='NODE:.metadata.name,MEMORY:.status.allocatable.memory'
kubectl delete namespace demo-pending --wait --timeout=120s
```

```text title="Вывод"
namespace/demo-pending created
pod/big created
--- get pods
NAME   READY   STATUS    RESTARTS   AGE
big    0/1     Pending   0          0s
--- events
REASON             MESSAGE
FailedScheduling   0/1 nodes are available: 1 Insufficient memory. preemption: 0/1 nodes …
--- node allocatable memory
NODE                      MEMORY
reference-control-plane   8125932Ki
namespace "demo-pending" deleted
```

Признак — событие `FailedScheduling` от `default-scheduler`: `0/1 nodes are available: 1 Insufficient memory` (узлов один, и на нём не хватает памяти). Читать это как «не хватает свободных requests», а не «узел занят»: планировщик сравнивает запрошенное с тем, что ещё не зарезервировано другими подами, а не с текущей загрузкой. В песочнице память узла — память виртуальной машины Docker Desktop.

Другая причина `Pending`, которую называет документация, — селектор меток узла, под который не подходит ни один узел; она тоже видна в событиях планировщика. Лечение зависит от причины: уменьшить `requests` до реальной потребности, добавить узел или убрать нерабочее условие размещения (документация предлагает удалить поды, поправить requests или добавить узлы).

### Частые ошибки в ответе

- «Под запустится сам, когда освободится место». Если запрос больше свободного на любом узле, ничего не освободится; запрос надо уменьшить или кластер расширить.
- «`Pending` с `FailedScheduling` — значит, образ скачивается». `FailedScheduling` значит, что под не размещён ни на один узел; скачивание образа начинается только после размещения (в колонке `STATUS` оно видно как `ContainerCreating`, хотя фаза пода всё ещё `Pending`).
- «Подниму `limits`». Планировщик смотрит на `requests`, лимиты на размещение не влияют.

## ImagePullBackOff

Kubelet не смог скачать образ: неверное имя или тег, образа нет в реестре, нет прав на приватный реестр (`imagePullSecret`). Состояние `ErrImagePull` — сама неудачная попытка, `ImagePullBackOff` — ожидание перед следующей, с растущей задержкой до 5 минут (по документации Kubernetes).

Опыт: образ `reference-app:404`, которого нет. Тег не `latest`, а политика `IfNotPresent`, но на узле такого образа нет, и kubelet идёт скачивать его с публичного реестра (обращение к Docker Hub, поэтому нужна сеть). Блок печатает каждую смену состояния.

```sh title="imagepull.sh"
kubectl delete namespace demo-image --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-image
kubectl apply -n demo-image -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: nope
spec:
  containers:
    - name: app
      image: reference-app:404
      imagePullPolicy: IfNotPresent
YAML
short() { awk '{ if (length($0) > 90) print substr($0, 1, 89) "…"; else print }'; }
last=""
ok=""
for _ in $(seq 60); do
  s="$(kubectl get pod nope -n demo-image -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}')"
  [ "$s" != "$last" ] && echo "status: $s" && last="$s"
  [ "$s" = ImagePullBackOff ] && { ok=1; break; }
  sleep 1
done
[ -n "$ok" ] || { echo "timeout: no ImagePullBackOff" >&2; exit 1; }
echo "--- get pods"
kubectl get pods -n demo-image
echo "--- events"
kubectl get events -n demo-image --sort-by=.lastTimestamp \
  -o custom-columns='TYPE:.type,REASON:.reason,MESSAGE:.message' | short
kubectl delete namespace demo-image --wait --timeout=120s
```

```text title="Вывод"
namespace/demo-image created
pod/nope created
status: ContainerCreating
status: ErrImagePull
status: ImagePullBackOff
--- get pods
NAME   READY   STATUS             RESTARTS   AGE
nope   0/1     ImagePullBackOff   0          18s
--- events
TYPE      REASON      MESSAGE
Normal    Scheduled   Successfully assigned demo-image/nope to reference-control-plane
Warning   Failed      Failed to pull image "reference-app:404": failed to pull and unpack…
Warning   Failed      Error: ErrImagePull
Normal    BackOff     Back-off pulling image "reference-app:404"
Warning   Failed      Error: ImagePullBackOff
Normal    Pulling     Pulling image "reference-app:404"
namespace "demo-image" deleted
```

Состояние прошло путь `ContainerCreating` → `ErrImagePull` → `ImagePullBackOff`. Событие `Failed` с `Failed to pull image "reference-app:404"` называет образ и причину; точный текст зависит от реестра и сети (в полном тексте события, обрезанном в выводе, в песочнице стоит `pull access denied, repository does not exist or may require authorization`). Документация предлагает проверить три вещи: верно ли имя образа, загружен ли он в реестр и скачивается ли он вручную (`docker pull`), а для приватного реестра ещё и `imagePullSecret`. Лечение — исправить образ или тег в манифесте, а не перезапускать под: он снова упрётся в то же.

### Частые ошибки в ответе

- «Перезапущу под, может, сеть мигнула». Опечатка в теге или отсутствующий образ не пройдут от перезапуска; сеть проверяют по тексту события.
- «`imagePullPolicy: Always` лечит». Политика определяет, когда kubelet ходит в реестр, и не создаёт образа, которого там нет.

## Под Running, но не Ready

Контейнер работает, но readiness-проба проваливается: колонка `READY` показывает `0/1`, а `STATUS` остаётся `Running`. Рестартов нет: провал readiness контейнер не перезапускает, он лишь выводит под из раздачи трафика (перезапускает liveness-проба, см. [[Деплой в Kubernetes#Три пробы]]). По документации Kubernetes, EndpointSlice-контроллер при провале пробы убирает IP пода из EndpointSlice всех подходящих Service; в прогоне ниже адрес остаётся в EndpointSlice, но с условием `ready=false`, то есть как цель для трафика он не годится. Как это сказывается на выкатке, показано в [[Деплой в Kubernetes#Readiness и выкатка]]; здесь показан признак.

Опыт: под с `NOT_READY=1` и readiness-пробой на `/ready`, перед ним Service.

```sh title="notready.sh"
kubectl delete namespace demo-notready --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-notready
kubectl apply -n demo-notready -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: sick
  labels: {app: sick}
spec:
  containers:
    - name: app
      image: reference-app:1
      imagePullPolicy: IfNotPresent
      env:
        - {name: NOT_READY, value: "1"}
      readinessProbe:
        httpGet: {path: /ready, port: 8080}
        periodSeconds: 2
---
apiVersion: v1
kind: Service
metadata:
  name: sick
spec:
  selector: {app: sick}
  ports:
    - port: 80
      targetPort: 8080
YAML
ok=""
for _ in $(seq 30); do
  kubectl get events -n demo-notready --field-selector reason=Unhealthy \
    -o custom-columns='MESSAGE:.message' | grep -q 'statuscode: 503' && { ok=1; break; }
  sleep 1
done
[ -n "$ok" ] || { echo "timeout: no readiness probe failure with 503" >&2; exit 1; }
echo "--- get pods"
kubectl get pods -n demo-notready
echo "--- phase and Ready condition"
kubectl get pod sick -n demo-notready -o jsonpath='phase={.status.phase} ready={.status.conditions[?(@.type=="Ready")].status}{"\n"}'
echo "--- events"
kubectl get events -n demo-notready --field-selector reason=Unhealthy \
  -o custom-columns='REASON:.reason,MESSAGE:.message' | grep -E 'REASON|statuscode: 503'
echo "--- EndpointSlice"
kubectl get endpointslice -n demo-notready -l kubernetes.io/service-name=sick \
  -o jsonpath='{range .items[*].endpoints[*]}ready={.conditions.ready} pod={.targetRef.name}{"\n"}{end}'
kubectl delete namespace demo-notready --wait --timeout=120s
```

```text title="Вывод"
namespace/demo-notready created
pod/sick created
service/sick created
--- get pods
NAME   READY   STATUS    RESTARTS   AGE
sick   0/1     Running   0          2s
--- phase and Ready condition
phase=Running ready=False
--- events
REASON      MESSAGE
Unhealthy   Readiness probe failed: HTTP probe failed with statuscode: 503
--- EndpointSlice
ready=false pod=sick
namespace "demo-notready" deleted
```

Признаки: `READY 0/1` при `Running` и нулевом `RESTARTS`, `Ready=False` в условиях пода, событие `Unhealthy` с `Readiness probe failed: HTTP probe failed with statuscode: 503`, а в EndpointSlice адрес пода есть, но `ready=false`. Раннее событие с `connection refused` в выводе отфильтровано: оно относится к первым секундам, пока приложение ещё не слушало порт, а вывод показывает постоянный провал. Что делать: смотреть, что отвечает приложение на пути пробы (логи, `describe`), и чинить причину: неготовая зависимость, неверный путь или порт пробы, слишком короткий таймаут.

### Частые ошибки в ответе

- «`Running` — значит, всё в порядке». `Running` говорит только о том, что процесс запущен; готовность к трафику показывает `READY` и условие `Ready`.
- «Раз `READY 0/1`, надо перезапустить под». Перезапуск не меняет ответ приложения на пробу; контейнер здоров с точки зрения kubelet и не перезапускается сам.
- «Ослаблю или уберу readiness-пробу, чтобы под стал Ready». Под получит трафик, пока не готов, и вместо диагностики вы получите 503 у пользователей ([[Деплой в Kubernetes#Readiness и выкатка]]).

## Что спросят дальше

- Сервис не отвечает, 502 во время выкатки, троттлинг и как зайти в под: [[Отладка сервисов и нагрузки в Kubernetes]].
- Как устроены requests, limits и QoS: [[Ресурсы и остановка в Kubernetes]].
- Как readiness управляет выкаткой и чем отличаются пробы: [[Деплой в Kubernetes]].
- Что стоит за `OOMKilled` на уровне ядра: [[Контейнеры изнутри]] и [[Память в Linux]].
