---
title: Деплой в Kubernetes
---

Как выкатить новую версию без простоя и что делать, если выкатка пошла не так: rolling update с `maxSurge` и `maxUnavailable`, readiness-проба как условие «выкатка прошла», застрявшая выкатка и откат, три вида проб и ошибки в их настройке. Всё показано прогоном в локальном кластере kind. Продолжение страниц «Объекты Kubernetes» и «Конфигурация и нагрузки в Kubernetes»; requests и limits, ConfigMap при деплое, HPA и остановку пода разбирает [[Ресурсы и остановка в Kubernetes]].

Как читать. Поднять песочницу: `make k8s-up`; блок запускается `make k8s-block PAGE="Docker и Kubernetes/Деплой в Kubernetes.md" TITLE="<имя>"`, вывод ниже снят именно так. В песочнице один узел kind (Kubernetes v1.37.0), образы `reference-app:1`, `reference-app:2` и `busybox:1.37.0` загружены в узел без реестра, поэтому в манифестах `imagePullPolicy: IfNotPresent`. Демо-приложение отвечает на `GET /` строкой `version=<V> pod=<имя пода>`, а переменные окружения управляют его поведением: `READY_DELAY_SECONDS=20` — первые 20 секунд после старта и `/`, и `/ready` отвечают `503 warming up`, `NOT_READY=1` — `/ready` отвечает 503 всегда. Блоки идут серией в namespace `demo-rollout`: первый блок создаёт его заново, последний удаляет, остальные подписаны «после блока `<имя>`». Имена подов, IP и возраст у вас будут другими.

Рядом: [[Объекты Kubernetes]], [[Конфигурация и нагрузки в Kubernetes]], [[Ресурсы и остановка в Kubernetes]], [[Миграции без простоя]].

## Вопросы на собеседовании

1. [Как работает rolling update и что значат maxSurge и maxUnavailable?](#rolling-update)
2. [Почему выкатка без readiness-пробы «успешна», а запросы получают ошибки?](#readiness-и-выкатка)
3. [Выкатка застряла: что произойдёт с сервисом и как откатить?](#застрявшая-выкатка-и-откат)
4. [Чем отличаются startup-, readiness- и liveness-проба и что бывает при ошибке в настройке?](#три-пробы)

## Rolling update

Deployment обновляет приложение через ReplicaSet: любое изменение шаблона пода (образ, переменные окружения, пробы) создаёт новый ReplicaSet, а контроллер по шагам увеличивает его и уменьшает старый. Сколько подов можно добавить сверх `replicas` и сколько можно потерять, задают два поля стратегии `RollingUpdate`:

- `maxSurge` — сколько подов сверх желаемого числа можно создать во время выкатки;
- `maxUnavailable` — сколько подов из желаемого числа могут быть недоступны.

Оба поля — число или процент, по умолчанию `25%`. Проценты округляются: `maxSurge` вверх, `maxUnavailable` вниз. Для трёх реплик это один дополнительный под и ноль недоступных: контроллер сначала создаёт новый под, ждёт, пока он станет доступным, и только потом убирает старый. Оба поля одновременно нулём быть не могут: API отклонит такой манифест, потому что выкатка не смогла бы сдвинуться с места.

Первый блок готовит сцену: Deployment `web` на `reference-app:1`, Service и под `busybox` для запросов изнутри кластера.

```sh title="rollout-setup.sh"
kubectl delete namespace demo-rollout --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-rollout
kubectl apply -n demo-rollout -f - <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 3
  selector:
    matchLabels: {app: web}
  template:
    metadata:
      labels: {app: web}
    spec:
      containers:
        - name: web
          image: reference-app:1
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8080
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: probe
data:
  probe.sh: |
    #!/bin/sh
    # probe.sh URL COUNT NAME: раз в секунду GET по URL, коды ответа в /tmp/NAME.codes
    url=$1; n=$2; name=$3; i=0
    : > "/tmp/$name.codes"
    while [ "$i" -lt "$n" ]; do
      wget -S -O /dev/null -T 2 "$url" 2>&1 \
        | awk '/^ *HTTP\//{c=$2} END{print c ? c : "err"}' >> "/tmp/$name.codes"
      i=$((i + 1)); sleep 1
    done
    touch "/tmp/$name.done"
---
apiVersion: v1
kind: Pod
metadata:
  name: client
spec:
  containers:
    - name: client
      image: busybox:1.37.0
      imagePullPolicy: IfNotPresent
      command: ["sleep", "3600"]
      volumeMounts:
        - name: probe
          mountPath: /scripts
  volumes:
    - name: probe
      configMap:
        name: probe
        defaultMode: 0755
YAML
kubectl expose deployment web -n demo-rollout --port=80 --target-port=8080
kubectl rollout status deployment/web -n demo-rollout --timeout=90s | tail -1
kubectl wait pod/client -n demo-rollout --for=condition=Ready --timeout=60s
kubectl get deployment web -n demo-rollout -o jsonpath='{.spec.strategy}{"\n"}'
kubectl exec client -n demo-rollout -- wget -q -O- http://web/
```

```text title="Вывод"
namespace/demo-rollout created
deployment.apps/web created
configmap/probe created
pod/client created
service/web exposed
deployment "web" successfully rolled out
pod/client condition met
{"rollingUpdate":{"maxSurge":"25%","maxUnavailable":"25%"},"type":"RollingUpdate"}
version=1 pod=web-67fb57f565-dgqzc
```

Стратегия по умолчанию видна в объекте, хотя в манифесте её не было. Скрипт `probe.sh` из ConfigMap пригодится ниже: он раз в секунду обращается к адресу и складывает коды ответа в файл. Теперь выкатим вторую версию и посмотрим, как контроллер двигал ReplicaSet.

```sh title="rollout-update.sh"
kubectl set image deployment/web web=reference-app:2 -n demo-rollout
kubectl rollout status deployment/web -n demo-rollout --timeout=90s | tail -1
kubectl get rs -n demo-rollout -l app=web \
  -o custom-columns='NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas,IMAGE:.spec.template.spec.containers[0].image'
kubectl get events -n demo-rollout --sort-by=.metadata.creationTimestamp \
  --field-selector involvedObject.kind=Deployment,involvedObject.name=web \
  -o custom-columns='MESSAGE:.message'
kubectl exec client -n demo-rollout -- wget -q -O- http://web/
```

```text title="Вывод"
deployment.apps/web image updated
deployment "web" successfully rolled out
NAME             DESIRED   READY    IMAGE
web-5f7dfccf4d   3         3        reference-app:2
web-67fb57f565   0         <none>   reference-app:1
MESSAGE
Scaled up replica set web-67fb57f565 from 0 to 3
Scaled up replica set web-5f7dfccf4d from 0 to 1
Scaled down replica set web-67fb57f565 from 3 to 2
Scaled up replica set web-5f7dfccf4d from 1 to 2
Scaled down replica set web-67fb57f565 from 2 to 1
Scaled up replica set web-5f7dfccf4d from 2 to 3
Scaled down replica set web-67fb57f565 from 1 to 0
version=2 pod=web-5f7dfccf4d-6472n
```

Блок выполняется после блока `rollout-setup.sh`. Старый ReplicaSet остался с `DESIRED 0`: он нужен для отката. События Deployment показывают чередование: новый ReplicaSet растёт на один под, старый уменьшается на один, и так до конца. В каждый момент доступны минимум три пода, а всего их не больше четырёх.

### Частые ошибки в ответе

- «Deployment управляет подами напрямую». Он управляет ReplicaSet, а те — подами; поэтому откат возможен: старый ReplicaSet никуда не делся.
- «Выкатка пересоздаёт все поды разом». Это стратегия `Recreate`, она осознанно даёт простой. `RollingUpdate` меняет поды частями в рамках `maxSurge` и `maxUnavailable`.
- «Обновление образа с тем же тегом (`latest`) запустит выкатку». Шаблон пода не изменился, значит, нового ReplicaSet не будет; в манифесте нужен конкретный тег (или digest), и тег `latest` в манифесте — плохая практика сам по себе.
- «`kubectl set image` или `kubectl edit` на проде — нормальный способ выкатки». Они меняют живой объект мимо репозитория; настоящий манифест и кластер расходятся, и следующий деплой молча откатит правку. Выкатка на проде — это изменение манифеста в репозитории.

## Readiness и выкатка

Контроллер считает новый под доступным, когда он `Ready`. Без readiness-пробы под становится `Ready`, как только запустился контейнер, то есть задолго до того, как приложение готово отвечать. Тогда `rollout status` сообщает об успехе, старые поды уже убраны, а Service отправляет запросы на прогревающиеся поды. Демо-приложение с `READY_DELAY_SECONDS=20` играет роль такого приложения: первые 20 секунд оно отвечает `503 warming up`.

Первый опыт — без пробы. Фоновый цикл в поде `client` считает коды ответа сервиса `nogate` с начала выкатки, а сам Deployment обновляется до `reference-app:2` с прогревом 20 секунд.

```sh title="rollout-no-readiness.sh"
kubectl apply -n demo-rollout -f - <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nogate
spec:
  replicas: 2
  selector:
    matchLabels: {app: nogate}
  template:
    metadata:
      labels: {app: nogate}
    spec:
      containers:
        - name: web
          image: reference-app:1
          imagePullPolicy: IfNotPresent
YAML
kubectl expose deployment nogate -n demo-rollout --port=80 --target-port=8080
kubectl rollout status deployment/nogate -n demo-rollout --timeout=90s | tail -1
kubectl exec client -n demo-rollout -- \
  sh -c '/scripts/probe.sh http://nogate 45 nogate >/dev/null 2>&1 &'
SECONDS=0
kubectl patch deployment nogate -n demo-rollout --patch \
  '{"spec":{"template":{"spec":{"containers":[{"name":"web","image":"reference-app:2","env":[{"name":"READY_DELAY_SECONDS","value":"20"}]}]}}}}'
kubectl rollout status deployment/nogate -n demo-rollout --timeout=90s | tail -1
echo "rollout finished in ${SECONDS}s"
for _ in $(seq 60); do
  kubectl exec client -n demo-rollout -- test -f /tmp/nogate.done 2>/dev/null && break
  sleep 2
done
echo "--- response codes during the rollout"
kubectl exec client -n demo-rollout -- sh -c 'sort /tmp/nogate.codes | uniq -c'
kubectl delete deployment/nogate service/nogate -n demo-rollout
```

```text title="Вывод"
deployment.apps/nogate created
service/nogate exposed
deployment "nogate" successfully rolled out
deployment.apps/nogate patched
deployment "nogate" successfully rolled out
rollout finished in 1s
--- response codes during the rollout
     25 200
     19 503
      1 err
deployment.apps "nogate" deleted from demo-rollout namespace
service "nogate" deleted from demo-rollout namespace
```

Блок выполняется после блока `rollout-setup.sh`. `rollout status` объявил успех через секунду после патча, а сервис ещё около 20 секунд отвечал `503`. Для клиента это та же авария, что и падение сервиса, хотя ни один под не падал. Строка `err` — запрос, на который не пришёл ответ за 2 секунды (`wget -T 2`); такие единичные запросы в одних прогонах есть, в других нет.

Второй опыт — то же самое, но с readiness-пробой на `/ready`. Теперь под не получает трафик, пока проба не пройдёт, а контроллер не убирает старый под, пока новый не `Ready`.

```sh title="rollout-readiness.sh"
kubectl apply -n demo-rollout -f - <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gated
spec:
  replicas: 2
  selector:
    matchLabels: {app: gated}
  template:
    metadata:
      labels: {app: gated}
    spec:
      containers:
        - name: web
          image: reference-app:1
          imagePullPolicy: IfNotPresent
          readinessProbe: {httpGet: {path: /ready, port: 8080}, periodSeconds: 2}
YAML
kubectl expose deployment gated -n demo-rollout --port=80 --target-port=8080
kubectl rollout status deployment/gated -n demo-rollout --timeout=90s | tail -1
kubectl exec client -n demo-rollout -- \
  sh -c '/scripts/probe.sh http://gated 75 gated >/dev/null 2>&1 &'
SECONDS=0
kubectl patch deployment gated -n demo-rollout --patch \
  '{"spec":{"template":{"spec":{"containers":[{"name":"web","image":"reference-app:2","env":[{"name":"READY_DELAY_SECONDS","value":"20"}]}]}}}}'
kubectl rollout status deployment/gated -n demo-rollout --timeout=120s | tail -1
echo "rollout finished in ${SECONDS}s"
for _ in $(seq 60); do
  kubectl exec client -n demo-rollout -- test -f /tmp/gated.done 2>/dev/null && break
  sleep 2
done
echo "--- response codes during the rollout"
kubectl exec client -n demo-rollout -- sh -c 'sort /tmp/gated.codes | uniq -c'
kubectl delete deployment/gated service/gated -n demo-rollout
```

```text title="Вывод"
deployment.apps/gated created
service/gated exposed
deployment "gated" successfully rolled out
deployment.apps/gated patched
deployment "gated" successfully rolled out
rollout finished in 42s
--- response codes during the rollout
     75 200
deployment.apps "gated" deleted from demo-rollout namespace
service "gated" deleted from demo-rollout namespace
```

Блок выполняется после блока `rollout-setup.sh`. Выкатка стала дольше: каждый под ждёт прогрева, а на сервис не ушёл ни один `503`. Время выкатки — цена за то, что запросы не теряются. Если проба задана, а приложение стартует дольше, чем рассчитано, выкатка будет застревать, и следующий раздел показывает, как это выглядит.

### Частые ошибки в ответе

- «`rollout status` успешен, значит, сервис работает». Он проверяет только `Ready` подов; без readiness-пробы это значит «контейнер запущен».
- «Liveness-проба решает, идёт ли на под трафик». Это делает readiness: непрошедший под убирают из адресов Service, но не перезапускают. Liveness перезапускает контейнер.
- «Readiness-проба нужна только для долгого старта». Она нужна и позже: приложение, у которого пропала зависимость или переполнена очередь, может временно выйти из балансировки и вернуться.
- «Достаточно фиксированной паузы (`initialDelaySeconds` или `sleep` в entrypoint)». Пауза либо слишком короткая, либо тормозит каждый деплой; проверка готовности по факту точнее.

## Застрявшая выкатка и откат

Если новые поды не становятся `Ready`, а `maxUnavailable: 0`, контроллер не убирает старые: сервис продолжает работать на прежней версии, но выкатка не заканчивается. Через `progressDeadlineSeconds` (по умолчанию 600 секунд) Deployment получает условие `Progressing=False` с причиной `ProgressDeadlineExceeded`, но сам ничего не откатывает.

Опыт: Deployment `stuck` на `reference-app:1` с `maxUnavailable: 0`, затем выкатка `reference-app:2` с `NOT_READY=1`, то есть с версией, у которой `/ready` не проходит никогда.

```sh title="rollout-stuck.sh"
kubectl apply -n demo-rollout -f - <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: stuck
  annotations:
    kubernetes.io/change-cause: "v1, reference-app:1"
spec:
  replicas: 3
  strategy:
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels: {app: stuck}
  template:
    metadata:
      labels: {app: stuck}
    spec:
      containers:
        - name: web
          image: reference-app:1
          imagePullPolicy: IfNotPresent
          readinessProbe: {httpGet: {path: /ready, port: 8080}, periodSeconds: 2}
YAML
kubectl expose deployment stuck -n demo-rollout --port=80 --target-port=8080
kubectl rollout status deployment/stuck -n demo-rollout --timeout=90s | tail -1
kubectl patch deployment stuck -n demo-rollout --patch \
  '{"metadata":{"annotations":{"kubernetes.io/change-cause":"v2, reference-app:2, NOT_READY=1"}},"spec":{"template":{"spec":{"containers":[{"name":"web","image":"reference-app:2","env":[{"name":"NOT_READY","value":"1"}]}]}}}}'
kubectl rollout status deployment/stuck -n demo-rollout --timeout=30s \
  || echo "rollout status exit code: $?"
kubectl get pods -n demo-rollout -l app=stuck \
  -o custom-columns='READY:.status.containerStatuses[0].ready,IMAGE:.spec.containers[0].image'
echo "--- 10 requests through the Service"
for _ in $(seq 10); do
  kubectl exec client -n demo-rollout -- wget -q -O- http://stuck/ | sed 's/ pod=.*//'
  echo
done | sort | uniq -c
```

```text title="Вывод"
deployment.apps/stuck created
service/stuck exposed
deployment "stuck" successfully rolled out
deployment.apps/stuck patched
Waiting for deployment "stuck" rollout to finish: 1 out of 3 new replicas have been updated...
error: timed out waiting for the condition
rollout status exit code: 1
READY   IMAGE
false   reference-app:2
true    reference-app:1
true    reference-app:1
true    reference-app:1
--- 10 requests through the Service
  10 version=1
```

Блок выполняется после блока `rollout-setup.sh`. Команда `rollout status --timeout` вышла по таймауту с кодом `1`: на этом коде строится проверка выкатки в CI. В списке три старых пода `Ready` и один новый `false`, а десять запросов получили `version=1`. Так и должно быть: плохая версия не взяла на себя трафик, поэтому застрявшая выкатка опасна не простоем, а тем, что про неё забывают.

Теперь история ревизий и откат.

```sh title="rollout-undo.sh"
kubectl rollout history deployment/stuck -n demo-rollout
kubectl rollout undo deployment/stuck -n demo-rollout 2>&1 | cut -c1-88
kubectl rollout status deployment/stuck -n demo-rollout --timeout=90s | tail -1
kubectl rollout history deployment/stuck -n demo-rollout
kubectl get rs -n demo-rollout -l app=stuck \
  -o custom-columns='DESIRED:.spec.replicas,READY:.status.readyReplicas,IMAGE:.spec.template.spec.containers[0].image'
kubectl delete deployment/stuck service/stuck -n demo-rollout
```

```text title="Вывод"
deployment.apps/stuck 
REVISION  CHANGE-CAUSE
1         v1, reference-app:1
2         v2, reference-app:2, NOT_READY=1

Warning: resource deployments/stuck was previously managed with 'kubectl apply'. Rolling
deployment.apps/stuck rolled back
deployment "stuck" successfully rolled out
deployment.apps/stuck 
REVISION  CHANGE-CAUSE
2         v2, reference-app:2, NOT_READY=1
3         v1, reference-app:1

DESIRED   READY    IMAGE
0         <none>   reference-app:2
3         3        reference-app:1
deployment.apps "stuck" deleted from demo-rollout namespace
service "stuck" deleted from demo-rollout namespace
```

Блок выполняется после блока `rollout-stuck.sh`. Предупреждение `rollout undo` про `last-applied-configuration` длинное и обрезано командой `cut` до ширины страницы; ошибки отката оно не скрывает. `rollout undo` вернул шаблон предыдущей ревизии, и она получила новый номер: откат — это ещё одна выкатка, только на старый ReplicaSet. История хранится в старых ReplicaSet, их число ограничено `revisionHistoryLimit` (по умолчанию 10), а колонка `CHANGE-CAUSE` берётся из аннотации `kubernetes.io/change-cause`.

На проде откат — это откат манифеста в репозитории: коммит `revert`, и обычный конвейер выкатывает прежнее состояние. `kubectl rollout undo` меняет живой объект мимо репозитория; GitOps-контроллер вернёт «плохую» версию обратно, а вручную откатанное состояние никто не увидит. Команда годится в песочнице и как аварийная мера, о которой потом обязательно говорят команде.

### Частые ошибки в ответе

- «Застрявшая выкатка уложит сервис». С `maxUnavailable: 0` и readiness-пробой старые поды остаются; сервис ляжет, если пробы нет, потому что тогда плохой под примет трафик.
- «Kubernetes сам откатит неудачную выкатку». Он только помечает `ProgressDeadlineExceeded`; откат делает человек или внешний конвейер по коду выхода `rollout status`.
- «Лечим `CrashLoopBackOff` командой `kubectl delete pod`». Контроллер создаст такой же под из того же шаблона, и причина останется; сначала логи и события (см. «Отладка в Kubernetes»).
- «Откат на проде — `kubectl rollout undo`». В репозитории остаётся плохая версия; откат делают изменением манифеста.

## Три пробы

- `readiness` отвечает на вопрос «можно ли слать трафик». При провале под убирают из адресов Service, контейнер не перезапускают.
- `liveness` отвечает на вопрос «жив ли процесс». При провале kubelet перезапускает контейнер.
- `startup` отвечает на вопрос «закончился ли запуск». Пока она не прошла, остальные пробы не выполняются. Это замена огромному `initialDelaySeconds` для приложений с долгим стартом.

Ошибка настройки: liveness-проба на `/ready` и приложение, которому на прогрев нужно больше времени, чем `failureThreshold × periodSeconds`. Здесь проверка раз в 5 секунд и три неудачи подряд убивают контейнер через 15 секунд, а прогрев занимает 30. После перезапуска прогрев начинается заново, и цикл не закончится никогда.

```sh title="probe-startup.sh"
kubectl apply -n demo-rollout -f - <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: probe-bad
spec:
  replicas: 1
  selector:
    matchLabels: {app: probe-bad}
  template:
    metadata:
      labels: {app: probe-bad}
    spec:
      containers:
        - name: web
          image: reference-app:1
          imagePullPolicy: IfNotPresent
          env:
            - {name: READY_DELAY_SECONDS, value: "30"}
          readinessProbe: {httpGet: {path: /ready, port: 8080}, periodSeconds: 5}
          livenessProbe: {httpGet: {path: /ready, port: 8080}, periodSeconds: 5, failureThreshold: 3}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: probe-good
spec:
  replicas: 1
  selector:
    matchLabels: {app: probe-good}
  template:
    metadata:
      labels: {app: probe-good}
    spec:
      containers:
        - name: web
          image: reference-app:1
          imagePullPolicy: IfNotPresent
          env:
            - {name: READY_DELAY_SECONDS, value: "30"}
          readinessProbe: {httpGet: {path: /ready, port: 8080}, periodSeconds: 5}
          startupProbe: {httpGet: {path: /ready, port: 8080}, periodSeconds: 5, failureThreshold: 12}
          livenessProbe: {httpGet: {path: /ready, port: 8080}, periodSeconds: 5, failureThreshold: 3}
YAML
kubectl rollout status deployment/probe-good -n demo-rollout --timeout=120s | tail -1
for _ in $(seq 60); do
  n=$(kubectl get pods -n demo-rollout -l app=probe-bad \
    -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')
  [ "${n:-0}" -ge 2 ] && break
  sleep 3
done
kubectl get pods -n demo-rollout -l 'app in (probe-bad,probe-good)' \
  -o custom-columns='APP:.metadata.labels.app,READY:.status.containerStatuses[0].ready,RESTARTS:.status.containerStatuses[0].restartCount'
bad=$(kubectl get pods -n demo-rollout -l app=probe-bad -o jsonpath='{.items[0].metadata.name}')
kubectl get events -n demo-rollout --field-selector "involvedObject.name=$bad" \
  -o custom-columns='REASON:.reason,MESSAGE:.message' --no-headers \
  | grep -E 'Liveness|Killing' | sort | uniq -c
kubectl delete namespace demo-rollout --wait --timeout=180s
```

```text title="Вывод"
deployment.apps/probe-bad created
deployment.apps/probe-good created
deployment "probe-good" successfully rolled out
APP          READY   RESTARTS
probe-bad    false   2
probe-good   true    0
   1 Killing     Container web failed liveness probe, will be restarted
   1 Unhealthy   Liveness probe failed: HTTP probe failed with statuscode: 503
namespace "demo-rollout" deleted
```

Блок выполняется после блока `rollout-setup.sh`. У `probe-bad` счётчик `RESTARTS` растёт и под не становится `Ready`, а события пишут, что проба не прошла и контейнер будет перезапущен. Приложение при этом исправно: ему просто не хватает времени. У `probe-good` тот же манифест с одной добавкой, startup-пробой: пока она не прошла, liveness не запускается. Она даёт до `12 × 5 = 60` секунд на запуск, и `RESTARTS` остаётся нулевым. Liveness на `/ready` оставлена только ради чистоты опыта: на практике liveness проверяет «процесс не завис» (лёгкий `/healthz`), а готовность принимает readiness. Последняя строка блока удаляет namespace серии.

### Частые ошибки в ответе

- «Liveness-проба должна проверять базу и соседние сервисы». Тогда падение базы убьёт перезапусками все поды сразу: перезапуск не чинит чужую зависимость, поды уходят в `CrashLoopBackOff`, а когда база вернётся, все прогреваются одновременно и сами создают нагрузку. Зависимость проверяет в крайнем случае readiness, и то осторожно: если она недоступна всем подам, у Service не останется адресов. Liveness отвечает только за состояние самого процесса.
- «Liveness на `/ready` — нормальный вариант». Прогрев и временные сбои превращаются в перезапуски; для этого есть startup-проба и отдельная liveness на лёгкий endpoint.
- «Для долгого старта поднимаем `initialDelaySeconds` до минут». Каждый старт ждёт весь срок даже при быстром запуске; startup-проба заканчивается сразу, как приложение готово.
- «Пробу можно не настраивать: контейнер ведь проверяется сам». Без проб kubelet знает лишь, жив ли процесс; завис ли он или готов ли принимать запросы, он не знает.

## Что спросят дальше

- Как требования к ресурсам (`requests`, `limits`) связаны с QoS и вытеснением, и что такое HPA — [[Ресурсы и остановка в Kubernetes]].
- Что произойдёт при остановке пода и зачем `preStop` с паузой: [[Сигналы в Linux#Kubernetes при остановке пода]].
- Как накатить миграцию базы вместе с выкаткой, чтобы старая и новая версии кода работали на одной схеме: [[Миграции без простоя]].
- Что делать, если под не стартует или не получает трафик: «Отладка в Kubernetes».
- Как контейнер получает лимиты на уровне ядра: [[Контейнеры изнутри]].
