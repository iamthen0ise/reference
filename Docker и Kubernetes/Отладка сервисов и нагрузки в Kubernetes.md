---
title: Отладка сервисов и нагрузки в Kubernetes
---

Продолжение страницы «Отладка в Kubernetes»: что делать, если под в порядке, а Service не отвечает; откуда берётся 502 при выкатке; как узнать, что приложение упирается в лимит CPU; и как зайти в под, чтобы посмотреть изнутри (`exec`, `debug`, `port-forward`). Порядок команд (`get`, `describe`, `logs`, события) и разбор падающих и не запускающихся подов — на первой странице: [[Отладка в Kubernetes]]. Всё здесь показано прогоном в локальном кластере kind.

Как читать. Блок запускается `make k8s-block PAGE="Docker и Kubernetes/Отладка сервисов и нагрузки в Kubernetes.md" TITLE="<имя>"`, вывод ниже снят именно так. В песочнице один узел kind (Kubernetes v1.37.0), образы `reference-app:1` и `busybox:1.37.0` загружены в узел без реестра, поэтому в манифестах `imagePullPolicy: IfNotPresent`; metrics-server стоит с флагом `--kubelet-insecure-tls` (особенность песочницы). Каждый сценарий работает в своём namespace `demo-<слаг>`: первый блок серии создаёт его заново, последний удаляет, остальные подписаны «после блока `<имя>`». Имена подов, IP и возраст у вас будут другими.

Рядом: [[Отладка в Kubernetes]], [[Деплой в Kubernetes]], [[Ресурсы и остановка в Kubernetes]], [[Объекты Kubernetes]], [[Контейнеры изнутри]].

## Вопросы на собеседовании

1. [Под Ready, а Service не отвечает: что проверять?](#service-не-отвечает)
2. [Откуда 502 во время выкатки и как их убрать?](#502-при-выкатке)
3. [Приложение стало медленным: как проверить, что это лимит CPU?](#медленно-и-троттлинг)
4. [Как попасть внутрь пода и что делать, если в образе нет оболочки?](#как-попасть-внутрь)

## Service не отвечает

Service находит поды по селектору меток и держит их адреса в EndpointSlice (устройство — в [[Объекты Kubernetes#Service и DNS]] и [[Объекты Kubernetes#Labels и selectors]]). Документация Kubernetes начинает отладку с этого: убедиться, что у Service есть endpoints (`kubectl get endpointslices -l kubernetes.io/service-name=<имя>`); если их нет, сверить метки подов с селектором, а если есть, проверить, что порт контейнера совпадает с `targetPort`.

Опыт: Deployment `web` с readiness-пробой и три Service перед ним. `good` — верный. `typo` — в селекторе опечатка (`app: wbe` вместо `app: web`). `badport` — селектор верный, но `targetPort: 8081`, а приложение слушает 8080. Долгоживущий под `client` из `busybox` ходит на каждый Service по имени.

```sh title="service-setup.sh"
kubectl delete namespace demo-svc --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-svc
kubectl apply -n demo-svc -f - <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 1
  selector:
    matchLabels: {app: web}
  template:
    metadata:
      labels: {app: web}
    spec:
      containers:
        - name: app
          image: reference-app:1
          imagePullPolicy: IfNotPresent
          readinessProbe:
            httpGet: {path: /ready, port: 8080}
            periodSeconds: 2
---
apiVersion: v1
kind: Service
metadata:
  name: good
spec:
  selector: {app: web}
  ports:
    - {port: 80, targetPort: 8080}
---
apiVersion: v1
kind: Service
metadata:
  name: typo
spec:
  selector: {app: wbe}
  ports:
    - {port: 80, targetPort: 8080}
---
apiVersion: v1
kind: Service
metadata:
  name: badport
spec:
  selector: {app: web}
  ports:
    - {port: 80, targetPort: 8081}
---
apiVersion: v1
kind: Pod
metadata:
  name: client
spec:
  containers:
    - name: c
      image: busybox:1.37.0
      imagePullPolicy: IfNotPresent
      command: ["sleep", "3600"]
YAML
kubectl rollout status deployment/web -n demo-svc --timeout=90s
kubectl wait pod/client -n demo-svc --for=condition=Ready --timeout=60s
for s in good typo badport; do
  echo "--- $s"
  kubectl exec client -n demo-svc -- wget -qO- -T 3 "http://$s" 2>&1 || true
  echo
done
```

```text title="Вывод"
namespace/demo-svc created
deployment.apps/web created
service/good created
service/typo created
service/badport created
pod/client created
Waiting for deployment "web" rollout to finish: 0 of 1 updated replicas are available...
deployment "web" successfully rolled out
pod/client condition met
--- good
version=1 pod=web-76c8b5f6d-g42mt
--- typo
wget: can't connect to remote host (10.96.192.143): Connection refused
command terminated with exit code 1

--- badport
wget: can't connect to remote host (10.96.177.64): Connection refused
command terminated with exit code 1
```

Оба сломанных Service дают одинаковый симптом: `Connection refused` (так было в песочнице), и по нему различить причины нельзя. Различает EndpointSlice. Второй блок идёт после блока `service-setup.sh`, показывает селекторы, метки пода и адреса с портами и удаляет namespace.

```sh title="service-inspect.sh"
echo "--- selectors"
kubectl get svc -n demo-svc -o custom-columns='SERVICE:.metadata.name,SELECTOR:.spec.selector'
echo "--- pod labels"
kubectl get pods -n demo-svc -l app=web --show-labels
echo "--- endpoints per service"
for s in good typo badport; do
  addr="$(kubectl get endpointslice -n demo-svc -l kubernetes.io/service-name=$s \
    -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]}{end}')"
  port="$(kubectl get endpointslice -n demo-svc -l kubernetes.io/service-name=$s \
    -o jsonpath='{.items[0].ports[0].port}')"
  echo "$s: address=${addr:-none} port=${port:-none}"
done
kubectl delete namespace demo-svc --wait --timeout=120s
```

```text title="Вывод"
--- selectors
SERVICE   SELECTOR
badport   map[app:web]
good      map[app:web]
typo      map[app:wbe]
--- pod labels
NAME                  READY   STATUS    RESTARTS   AGE   LABELS
web-76c8b5f6d-g42mt   1/1     Running   0          4s    app=web,pod-template-hash=76c8b5f6d
--- endpoints per service
good: address=10.244.0.22 port=8080
typo: address=none port=none
badport: address=10.244.0.22 port=8081
namespace "demo-svc" deleted
```

У `typo` адресов нет: селектор `app=wbe` не совпадает ни с одним подом (метка пода `app=web`), и Service ни на кого не смотрит. У `badport` адрес пода есть, а порт в EndpointSlice — 8081: Service честно шлёт трафик на порт, на котором никто не слушает. Правило такое: нет адресов — сравнить селектор с метками пода; адрес есть, а ответа нет — сравнить порт в EndpointSlice с портом, который слушает приложение, и проверить, что под `Ready` ([[Отладка в Kubernetes#Под Running, но не Ready]]).

### Частые ошибки в ответе

- «Сервис сломан, пересоздам его». Пересозданный Service с той же опечаткой снова останется без адресов; сначала `get endpointslice`.
- «Под `Running`, значит Service должен работать». Нужны ещё совпадение меток с селектором, `Ready` и верный `targetPort`.
- «Нет ответа — значит сеть кластера». В обоих сценариях сеть исправна; причина в конфигурации Service, и находится она за две команды на чтение.
- «Разберусь через `kubectl edit svc` на проде». Правка живого объекта расходится с манифестом в репозитории, и следующий деплой вернёт опечатку.

## 502 при выкатке

502 пользователь видит не от Kubernetes, а от прокси или балансировщика перед подами (ingress, облачный балансировщик): он не смог получить ответ от пода, на который его направили. Во время выкатки это случается по двум причинам.

Первая — у нового пода нет readiness-пробы, и он получает трафик, пока приложение прогревается. Это уже показано в [[Деплой в Kubernetes#Readiness и выкатка]] (там по пути до ответа 503 попадаются и ошибки соединения), здесь не повторяется.

Вторая — под останавливается без паузы. Адрес пода убирают из раздачи, и параллельно kubelet начинает его останавливать. В документации про EndpointSlice сказано, что условие `terminating` выставляется, когда под впервые удалён (получил `deletionTimestamp`), скорее всего до выхода контейнеров, и что сервисные прокси обычно игнорируют такие адреса. Но до балансировщиков и ingress-контроллеров эта новость доходит не мгновенно: пока она идёт, запрос может прийти к уже остановленному приложению. Без `preStop` (или паузы в самом приложении) `TERM` приходит сразу, и приложение закрывает порт ([[Сигналы в Linux#Kubernetes при остановке пода]]); `preStop` с паузой оттягивает `TERM` ([[Ресурсы и остановка в Kubernetes#Остановка пода]]).

Потерянные запросы в песочнице получить не вышло: в прогоне ниже адрес стал `terminating` сразу, а `TERM` пришёл только через пару секунд, то есть порядок оказался благоприятным. Поэтому здесь показан механизм, а не потери: под без `preStop`, его адрес в EndpointSlice опрашивается каждые 0,1 секунды, а лог пода идёт с метками времени.

```sh title="stop-no-prestop.sh"
kubectl delete namespace demo-502 --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-502
kubectl apply -n demo-502 -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: nohook
  labels: {app: nohook}
spec:
  containers:
    - name: app
      image: reference-app:1
      imagePullPolicy: IfNotPresent
      readinessProbe:
        httpGet: {path: /ready, port: 8080}
        periodSeconds: 1
---
apiVersion: v1
kind: Service
metadata:
  name: nohook
spec:
  selector: {app: nohook}
  ports:
    - {port: 80, targetPort: 8080}
YAML
kubectl wait pod/nohook -n demo-502 --for=condition=Ready --timeout=60s
state() {
  kubectl get endpointslice -n demo-502 -l kubernetes.io/service-name=nohook \
    -o jsonpath='{range .items[*].endpoints[*]}ready={.conditions.ready} serving={.conditions.serving} terminating={.conditions.terminating}{end}'
}
kubectl logs -f --timestamps nohook -n demo-502 > "$PWD/nohook.log" &
logger=$!
echo "$(date -u +%H:%M:%S) kubectl delete pod"
kubectl delete pod nohook -n demo-502 --wait=false >/dev/null
prev=""
ok=""
for _ in $(seq 100); do
  s="$(state)"
  [ "$s" != "$prev" ] && echo "$(date -u +%H:%M:%S) endpoint: ${s:-none}" && prev="$s"
  [ -z "$s" ] && { ok=1; break; }
  sleep 0.1
done
[ -n "$ok" ] || { echo "timeout: endpoint did not disappear" >&2; exit 1; }
kubectl wait pod/nohook -n demo-502 --for=delete --timeout=60s
wait "$logger"
echo "--- pod log (UTC)"
cat nohook.log
kubectl delete namespace demo-502 --wait --timeout=120s
```

```text title="Вывод"
namespace/demo-502 created
pod/nohook created
service/nohook created
pod/nohook condition met
13:05:36 kubectl delete pod
13:05:36 endpoint: ready=false serving=true terminating=true
13:05:39 endpoint: none
pod/nohook condition met
--- pod log (UTC)
2026-11-21T13:05:38.840357511Z got TERM, shutting down
namespace "demo-502" deleted
```

Время из `date` — с точностью до секунды, из лога — с наносекундами, оба в UTC. В прогоне адрес сразу помечается `ready=false terminating=true`, затем исчезает, а `TERM` приходит через пару секунд после удаления. Чтобы появилась гонка, читатель списка адресов (ingress-контроллер, внешний балансировщик) должен отстать от kubelet; насколько он отстаёт, зависит от него и в песочнице не измерялось, а пауза в `preStop` нужна как запас на это отставание. Поэтому лечение двойное: readiness-проба у приложения (трафик только на готовые поды) и пауза перед остановкой (`preStop` с `sleep` или задержка закрытия порта в самом приложении), плюс `terminationGracePeriodSeconds`, который вмещает паузу и завершение работы.

### Частые ошибки в ответе

- «502 — это баг Kubernetes, надо увеличить число реплик». Реплики не убирают окно между остановкой пода и обновлением адресов у прокси; помогают проба и пауза при остановке.
- «Достаточно `terminationGracePeriodSeconds` побольше». Он только предел; без паузы и обработчика `TERM` под всё равно закроет порт сразу.
- «`kubectl delete pod --force --grace-period=0` ускорит выкатку». Он убирает объект из API, не дожидаясь подтверждения от kubelet (документация Kubernetes), и никакой паузы для обновления адресов у прокси не создаёт.
- «`preStop` с `sleep` можно в любом образе». В образе без `sleep` и оболочки хук не выполнится; подробнее — в [[Ресурсы и остановка в Kubernetes#Остановка пода]].

## Медленно и троттлинг

Лимит CPU не убивает процесс, а ограничивает его время: по документации Kubernetes, `limits.cpu` — жёсткий предел, который обеспечивает ядро, и контейнер не может использовать больше. Как квота работает в cgroup, разобрано в [[Контейнеры изнутри#Троттлинг CPU]]. Признак у приложения: оно «тормозит» при том, что снаружи узел свободен. Проверяют по порядку: `kubectl top pod` (расход рядом с лимитом?), затем `cpu.stat` внутри контейнера (растёт ли `nr_throttled`?).

Опыт: под с `CPU_BURN=1` (поток крутится в пустом цикле) и `limits.cpu: 200m`. Блок ждёт, пока metrics-server отдаст цифры, затем снимает `cpu.stat` дважды: второй раз, когда `nr_periods` вырос минимум на 30 (3 секунды при периоде 100 мс).

```sh title="throttle.sh"
kubectl delete namespace demo-cpu --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-cpu
kubectl apply -n demo-cpu -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: burner
spec:
  containers:
    - name: app
      image: reference-app:1
      imagePullPolicy: IfNotPresent
      env:
        - {name: CPU_BURN, value: "1"}
      resources:
        requests: {cpu: 100m}
        limits: {cpu: 200m}
YAML
kubectl wait pod/burner -n demo-cpu --for=condition=Ready --timeout=60s
ok=""
for _ in $(seq 40); do
  kubectl top pod burner -n demo-cpu --no-headers 2>/dev/null | grep -q . && { ok=1; break; }
  sleep 3
done
[ -n "$ok" ] || { echo "timeout: metrics-server returned no data" >&2; exit 1; }
echo "--- kubectl top pod"
kubectl top pod burner -n demo-cpu | sed 's/ *$//'
stat() { kubectl exec burner -n demo-cpu -- grep -E 'nr_periods|nr_throttled' /sys/fs/cgroup/cpu.stat | tr '\n' ' '; echo; }
echo "--- cpu.stat, twice"
first="$(stat)"
echo "$first"
base="$(echo "$first" | awk '{ print $2 }')"
ok=""
for _ in $(seq 40); do
  second="$(stat)"
  [ "$(echo "$second" | awk '{ print $2 }')" -ge $((base + 30)) ] && { ok=1; break; }
  sleep 1
done
[ -n "$ok" ] || { echo "timeout: nr_periods did not grow" >&2; exit 1; }
echo "$second"
echo "--- cpu.max"
kubectl exec burner -n demo-cpu -- cat /sys/fs/cgroup/cpu.max
kubectl delete namespace demo-cpu --wait --timeout=120s
```

```text title="Вывод"
namespace/demo-cpu created
pod/burner created
pod/burner condition met
--- kubectl top pod
NAME     CPU(cores)   MEMORY(bytes)
burner   200m         12Mi
--- cpu.stat, twice
nr_periods 157 nr_throttled 157 
nr_periods 194 nr_throttled 194 
--- cpu.max
20000 100000
namespace "demo-cpu" deleted
```

`kubectl top` показывает около `200m`: процесс упёрся в потолок. `cpu.max` внутри контейнера равен `20000 100000`: 20 мс CPU на каждые 100 мс, то есть 200m. Между двумя снимками `nr_periods` и `nr_throttled` выросли почти одинаково: почти в каждом периоде квота кончалась, и процесс ждал следующего. Значения у вас будут другими. Лечение: поднять лимит или убрать его, оставив `requests` (это решение политики кластера, см. [[Ресурсы и остановка в Kubernetes#Requests и limits]]), либо снизить расход в самом приложении; это не «добавить реплик», пока каждая упирается в тот же потолок.

### Частые ошибки в ответе

- «CPU на узле свободен, значит лимит не при чём». Квота не зависит от свободных ядер, это разобрано в [[Контейнеры изнутри#Троттлинг CPU]].
- «Лимит превышен, значит под убьют». CPU троттлится; убивают за память ([[Отладка в Kubernetes#OOMKilled]]).
- «`kubectl top` показывает 200m из 200m — всё штатно». Расход у потолка и есть признак того, что процесс может ждать квоты; подтверждает рост `nr_throttled`.
- «Сниму лимит CPU, чтобы не троттлилось, а потом разберёмся». Это допустимое решение лишь осознанное и на уровне политики: без лимита соседи делят остаток по `requests`, и шумный под сильнее влияет на них.

## Как попасть внутрь

Три способа, все для отладки, а не для постоянной работы:

- `kubectl exec` — выполнить команду в контейнере; годится, если в образе есть нужные утилиты.
- `kubectl debug` — добавить в работающий под эфемерный контейнер с нужными утилитами. По документации, он нужен, когда `exec` недостаточно: контейнер упал или в образе нет отладочных инструментов (например, distroless). Параметр `--target` указывает контейнер, чьё пространство процессов нужно увидеть; рантайм должен это поддерживать, иначе процессы чужого контейнера не видны. Эфемерный контейнер никогда не перезапускается автоматически, и для добавления нужно право на подресурс `pods/ephemeralcontainers`, которого нет в стандартных ролях `edit` и `admin`.
- `kubectl port-forward` — пробросить порт пода на локальную машину и ходить на него `curl` с локального хоста, минуя Service.

Опыт: под с приложением; в нём `exec` читает процесс 1; `debug` кладёт рядом `busybox` с `--target=app`, видит процесс приложения и ходит на `localhost`; наконец, `port-forward` в фоне и `curl` с локальной машины. Локальный порт выбирает сам `kubectl` (запись `:8080` вместо `порт:8080`, по документации Kubernetes), блок разбирает его из вывода. Фоновый `port-forward` останавливается в этом же блоке.

```sh title="debug.sh"
kubectl delete namespace demo-debug --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-debug
kubectl apply -n demo-debug -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: web
spec:
  containers:
    - name: app
      image: reference-app:1
      imagePullPolicy: IfNotPresent
YAML
kubectl wait pod/web -n demo-debug --for=condition=Ready --timeout=60s
short() { awk '{ if (length($0) > 90) print substr($0, 1, 89) "…"; else print }'; }
echo "--- exec"
kubectl exec web -n demo-debug -- sh -c 'id -un; python --version; tr "\0" " " < /proc/1/cmdline; echo'
echo "--- debug"
kubectl debug web -n demo-debug -c dbg --image=busybox:1.37.0 --target=app -- \
  sh -c 'ps | head -4; wget -qO- http://localhost:8080; echo' 2>&1 | short
ok=""
for _ in $(seq 30); do
  s="$(kubectl get pod web -n demo-debug -o jsonpath='{.status.ephemeralContainerStatuses[?(@.name=="dbg")].state.terminated.reason}')"
  [ -n "$s" ] && { ok=1; break; }
  sleep 1
done
[ -n "$ok" ] || { echo "timeout: ephemeral container did not finish" >&2; exit 1; }
kubectl logs web -n demo-debug -c dbg
echo "--- containers of the pod"
kubectl get pod web -n demo-debug -o jsonpath='containers={.spec.containers[*].name} ephemeral={.spec.ephemeralContainers[*].name}{"\n"}'
echo "--- port-forward"
kubectl port-forward -n demo-debug pod/web :8080 > pf.log &
pf=$!
ok=""
for _ in $(seq 40); do
  port="$(sed -n 's/^Forwarding from 127.0.0.1:\([0-9]*\) -> .*/\1/p' pf.log)"
  [ -n "$port" ] && curl -s -o /dev/null "localhost:$port/healthz" && { ok=1; break; }
  sleep 0.5
done
[ -n "$ok" ] || { echo "timeout: port-forward did not come up" >&2; kill "$pf"; exit 1; }
curl -s "localhost:$port/"; echo
kill "$pf"
wait "$pf" 2>/dev/null
kubectl delete namespace demo-debug --wait --timeout=120s
```

```text title="Вывод"
namespace/demo-debug created
pod/web created
pod/web condition met
--- exec
app
Python 3.13.18
python -u app.py 
--- debug
Targeting container "app". If you don't see processes from this container it may be becau…
PID   USER     TIME  COMMAND
    1 10001     0:00 python -u app.py
   20 root      0:00 sh -c ps | head -4; wget -qO- http://localhost:8080; echo
   30 root      0:00 ps
version=1 pod=web
--- containers of the pod
containers=app ephemeral=dbg
--- port-forward
version=1 pod=web
namespace "demo-debug" deleted
```

В `exec` видно, что процесс приложения работает от пользователя `app` и что это `python -u app.py`. Эфемерный контейнер `dbg` сначала печатает предупреждение kubectl про `--target`, затем `ps` показывает процесс `python -u app.py` с PID 1 рядом с собственными процессами `busybox`: пространство процессов общее. Из эфемерного контейнера `localhost:8080` отвечает, потому что сеть у пода общая. После выхода команды контейнер остаётся в `spec.ephemeralContainers` пода. В последнем разделе `curl` дошёл до приложения через `port-forward`, поэтому `pod=web` в ответе — имя пода, а процесс `kubectl` остановлен `kill` и `wait` в том же блоке. Номера PID у вас могут отличаться.

### Частые ошибки в ответе

- «Зайду в под и поправлю файл руками». Правка внутри контейнера пропадает при перезапуске и не попадает в образ; `exec` нужен, чтобы посмотреть и понять, а чинят в образе и манифесте.
- «В образе нет оболочки, значит отладить нельзя». Для этого и нужен эфемерный контейнер: `kubectl debug` с образом, в котором есть инструменты, и `--target`.
- «`kubectl exec` в каждый под по очереди — это отладка». Сначала события и логи ([[Отладка в Kubernetes#Общий порядок]]); в под заходят, когда они не объяснили причину.
- «`port-forward` — способ отдать сервис наружу». Он живёт, пока работает процесс `kubectl`, идёт с одного пода и годится только для проверки; доступ снаружи настраивают через Service и Ingress.

## Что спросят дальше

- С чего начать при любом сбое и как читать `CrashLoopBackOff`, `OOMKilled`, `Pending`, `ImagePullBackOff`: [[Отладка в Kubernetes]].
- Как выкатывать без потерь запросов: [[Деплой в Kubernetes]].
- Как остановка пода и `preStop` связаны с потерей запросов: [[Ресурсы и остановка в Kubernetes]], [[Сигналы в Linux]].
- Как квота CPU работает в ядре: [[Контейнеры изнутри]].
