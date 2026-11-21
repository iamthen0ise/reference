---
title: Ресурсы и остановка в Kubernetes
---

Что происходит с приложением после того, как манифест принят: requests и limits с классами QoS, смена ConfigMap и перезапуск подов, автомасштабирование HPA по CPU, остановка пода с `preStop` и место миграций базы и PodDisruptionBudget в деплое. Продолжение страницы «Деплой в Kubernetes»; всё показано прогоном в локальном кластере kind.

Как читать. Поднять песочницу: `make k8s-up`; блок запускается `make k8s-block PAGE="Docker и Kubernetes/Ресурсы и остановка в Kubernetes.md" TITLE="<имя>"`, вывод ниже снят именно так. В песочнице один узел kind (Kubernetes v1.37.0), образы `reference-app:1` и `busybox:1.37.0` загружены в узел без реестра, поэтому в манифестах `imagePullPolicy: IfNotPresent`. Metrics-server поставлен с флагом `--kubelet-insecure-tls`: это особенность песочницы, на настоящем кластере kubelet проверяют по сертификату. Память узла — это память виртуальной машины Docker Desktop. Каждый сценарий работает в своём namespace: его первый блок создаёт заново, последний удаляет. Имена подов, IP и возраст у вас будут другими.

Рядом: [[Деплой в Kubernetes]], [[Объекты Kubernetes]], [[Контейнеры изнутри]], [[Сигналы в Linux]].

## Вопросы на собеседовании

1. [Чем requests отличаются от limits и что такое классы QoS?](#requests-и-limits)
2. [Что будет с подами, если поменять ConfigMap?](#смена-configmap)
3. [Как работает HPA и от чего он считает загрузку?](#hpa-по-cpu)
4. [Что происходит с запросами при остановке пода и зачем нужен preStop?](#остановка-пода)
5. [Как выкатывать миграции базы вместе с приложением и что делает PodDisruptionBudget?](#миграции-и-poddisruptionbudget)

## Requests и limits

`requests` — сколько ресурсов резервирует scheduler при выборе узла: под не поместится на узел, где свободно меньше. `limits` — потолок, который применяет ядро через cgroups: память сверх лимита приводит к `OOMKilled`, CPU сверх лимита — к троттлингу, а не к убийству. Лимит CPU не гарантирует CPU: гарантию (точнее, долю при конкуренции) даёт `requests`. Как это выглядит на уровне ядра, разобрано в [[Контейнеры изнутри#cgroups v2 и память]] и [[Контейнеры изнутри#Троттлинг CPU]]; как читать `OOMKilled` и `Pending` из-за нехватки ресурсов — в «Отладка в Kubernetes».

По соотношению requests и limits Kubernetes присваивает поду класс QoS, он виден в `.status.qosClass`:

- `Guaranteed` — у каждого контейнера и CPU, и память заданы, `requests` равны `limits`;
- `Burstable` — задан хотя бы один request или limit, но условия `Guaranteed` не выполнены;
- `BestEffort` — не задано ничего.

Класс влияет на то, кого узел вытесняет первым при нехватке памяти: `BestEffort` и те `Burstable`, что превысили свои requests, раньше остальных.

```sh title="qos.sh"
kubectl delete namespace demo-resources --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-resources
kubectl apply -n demo-resources -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: besteffort
spec:
  containers:
    - name: c
      image: busybox:1.37.0
      imagePullPolicy: IfNotPresent
      command: ["sleep", "3600"]
---
apiVersion: v1
kind: Pod
metadata:
  name: burstable
spec:
  containers:
    - name: c
      image: busybox:1.37.0
      imagePullPolicy: IfNotPresent
      command: ["sleep", "3600"]
      resources:
        requests: {cpu: 100m, memory: 64Mi}
        limits: {memory: 128Mi}
---
apiVersion: v1
kind: Pod
metadata:
  name: guaranteed
spec:
  containers:
    - name: c
      image: busybox:1.37.0
      imagePullPolicy: IfNotPresent
      command: ["sleep", "3600"]
      resources:
        requests: {cpu: 100m, memory: 64Mi}
        limits: {cpu: 100m, memory: 64Mi}
YAML
kubectl wait pod --all -n demo-resources --for=condition=Ready --timeout=60s
kubectl get pods -n demo-resources -o custom-columns='NAME:.metadata.name,QOS:.status.qosClass'
```

```text title="Вывод"
namespace/demo-resources created
pod/besteffort created
pod/burstable created
pod/guaranteed created
pod/besteffort condition met
pod/burstable condition met
pod/guaranteed condition met
NAME         QOS
besteffort   BestEffort
burstable    Burstable
guaranteed   Guaranteed
```

`burstable` попал в средний класс, потому что у него нет лимита CPU и лимит памяти не равен request. Под без ресурсов вообще — первый кандидат на вытеснение, поэтому у рабочих сервисов `requests` задают всегда.

### Частые ошибки в ответе

- «`requests` — это лимит» или «`limits.cpu` гарантирует CPU». Request — резерв для планировщика и доля CPU при конкуренции, limit — потолок.
- «Превысил лимит CPU — под убьют». CPU троттлится (процесс получает меньше времени), убивают только за память.
- «Лимит памяти снимем, чтобы не падало». Без лимита процесс с утечкой съест память узла, и вытеснять начнут соседей; причину `OOMKilled` ищут в приложении (утечка, размер кучи), лимит поднимают осознанно.
- «Лимит CPU надо ставить как можно ниже». Слишком низкий лимит даёт троттлинг и рост задержек при свободных ядрах; многие команды задают `requests` и не задают лимит CPU, а решают это политикой кластера.

## Смена ConfigMap

Переменные окружения, взятые из ConfigMap, фиксируются при старте контейнера. Смена ConfigMap не перезапускает поды сама и не меняет env работающего процесса; файлы в томе kubelet обновит сам, с задержкой, но приложение должно их перечитать (разбор — в [[Конфигурация и нагрузки в Kubernetes]]).

```sh title="configmap-restart.sh"
kubectl apply -n demo-resources -f - <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: app
data:
  GREETING: hello
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: greeter
spec:
  replicas: 1
  selector:
    matchLabels: {app: greeter}
  template:
    metadata:
      labels: {app: greeter}
    spec:
      containers:
        - name: c
          image: busybox:1.37.0
          imagePullPolicy: IfNotPresent
          command: ["sleep", "3600"]
          envFrom:
            - configMapRef:
                name: app
YAML
kubectl rollout status deployment/greeter -n demo-resources --timeout=60s
echo "--- before the change"
kubectl exec deploy/greeter -n demo-resources -- printenv GREETING
kubectl patch configmap app -n demo-resources --patch '{"data":{"GREETING":"bonjour"}}'
echo "--- ConfigMap changed, pod untouched"
kubectl exec deploy/greeter -n demo-resources -- printenv GREETING
kubectl rollout restart deployment/greeter -n demo-resources
kubectl rollout status deployment/greeter -n demo-resources --timeout=60s
echo "--- after rollout restart"
kubectl exec deploy/greeter -n demo-resources -- printenv GREETING
kubectl delete namespace demo-resources --wait --timeout=120s
```

```text title="Вывод"
configmap/app created
deployment.apps/greeter created
Waiting for deployment "greeter" rollout to finish: 0 of 1 updated replicas are available...
deployment "greeter" successfully rolled out
--- before the change
hello
configmap/app patched
--- ConfigMap changed, pod untouched
hello
deployment.apps/greeter restarted
Waiting for deployment "greeter" rollout to finish: 1 old replicas are pending termination...
Waiting for deployment "greeter" rollout to finish: 1 old replicas are pending termination...
deployment "greeter" successfully rolled out
--- after rollout restart
bonjour
namespace "demo-resources" deleted
```

Блок выполняется после блока `qos.sh` и в конце удаляет его namespace. Процесс видел `hello` и после правки ConfigMap, а `rollout restart` создал новые поды с `bonjour`. На проде `rollout restart` не заменяет изменения манифеста: поды надо перезапускать самим изменением шаблона. Обычный приём — аннотация с хэшем конфига в шаблоне пода (пример, не прогоняется):

```yaml
spec:
  template:
    metadata:
      annotations:
        checksum/config: "3f0a…"   # хэш содержимого ConfigMap, считает конвейер
```

Хэш изменился — изменился шаблон пода — прошла обычная rolling-выкатка с пробами и откатом. Ту же роль играют неизменяемые ConfigMap с версией в имени (`app-v7`): имя в манифесте меняется вместе с содержимым.

### Частые ошибки в ответе

- «Поменял ConfigMap — приложение подхватило». Env не меняется до перезапуска, файлы приложение должно перечитать само.
- «`kubectl rollout restart` — способ выкатки конфигурации на проде». Он ничего не фиксирует в репозитории; конфиг и хэш в манифесте меняют коммитом.
- «Секрет можно положить в `ENV` Dockerfile или `ARG`». Он останется в слоях и истории образа; секрет подключают в под из Secret (см. [[Конфигурация и нагрузки в Kubernetes]]).

## HPA по CPU

HorizontalPodAutoscaler раз в цикл (по умолчанию 15 секунд) берёт метрику из metrics-server и считает желаемое число реплик: текущее число, умноженное на отношение текущей загрузки к целевой, с округлением вверх. Загрузка по CPU измеряется в процентах от `requests`, поэтому без `requests` HPA по CPU не работает. Число подов ограничено `minReplicas` и `maxReplicas`.

Опыт: Deployment `burn` с лимитом CPU 200m и `requests` 100m. Сначала приложение простаивает, затем `CPU_BURN=1` включает бесконечный цикл на одном потоке.

```sh title="hpa.sh"
kubectl delete namespace demo-scale --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-scale
kubectl apply -n demo-scale -f - <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: burn
spec:
  selector:
    matchLabels: {app: burn}
  template:
    metadata:
      labels: {app: burn}
    spec:
      containers:
        - name: burn
          image: reference-app:1
          imagePullPolicy: IfNotPresent
          resources:
            requests: {cpu: 100m, memory: 64Mi}
            limits: {cpu: 200m, memory: 128Mi}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: burn
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: burn
  minReplicas: 1
  maxReplicas: 3
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 50
YAML
kubectl rollout status deployment/burn -n demo-scale --timeout=90s
for _ in $(seq 40); do
  kubectl get hpa burn -n demo-scale -o jsonpath='{.status.currentMetrics}' | grep -q cpu && break
  sleep 3
done
echo "--- idle"
kubectl get hpa burn -n demo-scale
kubectl set env deployment/burn -n demo-scale CPU_BURN=1
kubectl rollout status deployment/burn -n demo-scale --timeout=90s
for _ in $(seq 60); do
  n=$(kubectl get hpa burn -n demo-scale -o jsonpath='{.status.currentReplicas}')
  [ "${n:-0}" -ge 3 ] && break
  sleep 3
done
echo "--- under load"
kubectl get hpa burn -n demo-scale
kubectl get events -n demo-scale --field-selector reason=SuccessfulRescale \
  -o custom-columns='MESSAGE:.message'
kubectl delete namespace demo-scale --wait --timeout=120s
```

```text title="Вывод"
namespace/demo-scale created
deployment.apps/burn created
horizontalpodautoscaler.autoscaling/burn created
Waiting for deployment "burn" rollout to finish: 0 of 1 updated replicas are available...
deployment "burn" successfully rolled out
--- idle
NAME   REFERENCE         TARGETS       MINPODS   MAXPODS   REPLICAS   AGE
burn   Deployment/burn   cpu: 1%/50%   1         3         1          31s
deployment.apps/burn env updated
Waiting for deployment "burn" rollout to finish: 1 old replicas are pending termination...
Waiting for deployment "burn" rollout to finish: 1 old replicas are pending termination...
deployment "burn" successfully rolled out
--- under load
NAME   REFERENCE         TARGETS         MINPODS   MAXPODS   REPLICAS   AGE
burn   Deployment/burn   cpu: 201%/50%   1         3         3          90s
MESSAGE
New size: 3; reason: cpu resource utilization (percentage of request) above target
namespace "demo-scale" deleted
```

Колонка `TARGETS` — текущая загрузка против цели. Под ограничен лимитом 200m, то есть двумя requests, поэтому загрузка упирается в `200%`, а HPA доводит число реплик до `maxReplicas`. Здесь же видна связь с троттлингом: подам не дают больше лимита, а HPA ориентируется на `requests`. Загрузка в песочнице зависит от процессора ВМ, поэтому точные проценты у вас могут отличаться.

### Частые ошибки в ответе

- «HPA считает загрузку от лимита». От `requests`; без них HPA по CPU не работает.
- «HPA ускоряет медленный запрос». Он добавляет реплики при росте суммарной нагрузки; узкое место в базе или в одном потоке он не лечит.
- «HPA и `replicas` в манифесте можно держать вместе». Когда HPA управляет Deployment, поле `replicas` из манифеста убирают, иначе каждая выкатка сбросит число реплик на значение из репозитория.
- «Масштабируем по CPU всё». Для очередей и I/O-нагрузки CPU не отражает перегрузку; нужны другие метрики (длина очереди, RPS).

## Остановка пода

Порядок остановки (`preStop`, `TERM`, `KILL`, общий срок `terminationGracePeriodSeconds`) описан в [[Сигналы в Linux#Kubernetes при остановке пода]] и здесь не повторяется. Здесь показано то, что там было только описанием: адрес под ещё в EndpointSlice, когда он уже завершается, а `TERM` приходит только после `preStop`.

Опыт: под с `preStop`, который пишет строку в лог и спит 10 секунд. Сервис выбирает под по метке; readiness-проба нужна, чтобы адрес был помечен готовым. Фоновая команда собирает лог пода с метками времени, пока под не исчезнет.

```sh title="graceful-stop.sh"
kubectl delete namespace demo-stop --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-stop
kubectl apply -n demo-stop -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: drain
  labels: {app: drain}
spec:
  containers:
    - name: app
      image: reference-app:1
      imagePullPolicy: IfNotPresent
      readinessProbe:
        httpGet: {path: /ready, port: 8080}
        periodSeconds: 1
      lifecycle:
        preStop:
          exec:
            command: ["sh", "-c", "echo preStop start > /proc/1/fd/1; sleep 10"]
---
apiVersion: v1
kind: Service
metadata:
  name: drain
spec:
  selector: {app: drain}
  ports:
    - port: 80
      targetPort: 8080
YAML
kubectl wait pod/drain -n demo-stop --for=condition=Ready --timeout=60s
show() {
  kubectl get endpointslice -n demo-stop -l kubernetes.io/service-name=drain \
    -o jsonpath='{range .items[*].endpoints[*]}ready={.conditions.ready} serving={.conditions.serving} terminating={.conditions.terminating}{"\n"}{end}'
}
echo "--- before delete"
show
kubectl logs -f --timestamps drain -n demo-stop > "$PWD/drain.log" &
logger=$!
kubectl delete pod drain -n demo-stop --wait=false
for _ in $(seq 20); do
  show | grep -q terminating=true && break
  sleep 0.5
done
echo "--- during preStop"
show
kubectl wait pod/drain -n demo-stop --for=delete --timeout=60s
wait "$logger"
echo "--- pod log"
cat drain.log
kubectl delete namespace demo-stop --wait --timeout=120s
```

```text title="Вывод"
namespace/demo-stop created
pod/drain created
service/drain created
pod/drain condition met
--- before delete
ready=true serving=true terminating=false
pod "drain" deleted from demo-stop namespace
--- during preStop
ready=false serving=true terminating=true
pod/drain condition met
--- pod log
2026-11-21T11:51:25.036738130Z preStop start
2026-11-21T11:51:35.049200260Z got TERM, shutting down
namespace "demo-stop" deleted
```

Пока работает `preStop`, под ещё в EndpointSlice, но помечен `ready=false` и `terminating=true`: в идеале новые соединения на него уже не идут (но снятие адреса доходит до kube-proxy и балансировщиков с задержкой, ради чего и нужен `sleep` в `preStop`), а `serving=true` значит, что он ещё может обслуживать уже принятые. Лог показывает порядок: `preStop` начался сразу, а `got TERM` пришёл на 10 секунд позже, когда хук закончился. Пауза в `preStop` даёт время, пока kube-proxy, ingress-контроллеры и балансировщики узнают, что адрес выбыл: за это время приложение ещё принимает запросы. Короткий `sleep` в песочнице одного узла надёжно не показывает потерю запросов, поэтому здесь показан механизм, а не вывод потерь.

В реальном сервисе пауза делается либо `preStop` с `sleep` (для образов с оболочкой или встроенным действием `sleep`), либо самим приложением: получив `TERM`, оно сначала продолжает принимать запросы ещё несколько секунд, потом закрывается. Срок `terminationGracePeriodSeconds` должен вмещать паузу плюс время завершения работы.

### Частые ошибки в ответе

- «Под ушёл из Service в момент `TERM`». Адрес убирается параллельно остановке и может ещё получать новые соединения: отсюда паузы в `preStop`.
- «`kubectl delete pod --force --grace-period=0` ускорит остановку». Он убирает объект из API, не дожидаясь kubelet; контейнер может ещё работать, а StatefulSet получит два пода с одним именем. Для зависшего пода сначала выясняют причину.
- «Достаточно увеличить `terminationGracePeriodSeconds`». Он лишь предел; без обработчика `TERM` каждый деплой дождётся его целиком и закончится `KILL`.
- «`preStop` с `sleep` можно в образе без оболочки». Команда `exec` выполняется внутри контейнера: нет `sleep`, нет и паузы. Для таких образов у хука есть встроенное действие `sleep`, его выполняет kubelet; с какой версии оно стабильно, сверьте по документации своего кластера.

## Миграции и PodDisruptionBudget

Миграция базы при деплое выполняется отдельным шагом до выкатки приложения: `Job` (в конвейере, до обновления Deployment, как хук релиза) или init-контейнер. Job запускается один раз, а init-контейнер — в каждом поде, поэтому при нескольких репликах миграции должны быть идемпотентными, иначе поды столкнутся на блокировках. Устройство Job и init-контейнеров показано в [[Конфигурация и нагрузки в Kubernetes]].

Главное — совместимость схемы. Во время rolling update старая и новая версии кода работают одновременно на одной базе, и откат приложения не откатывает схему. Поэтому миграции делают расширяющими: сначала добавить колонку или таблицу, выкатить код, который их использует, и только потом удалять старое. Как это делается без блокировок таблиц, разобрано в [[Миграции без простоя]].

PodDisruptionBudget ограничивает добровольные остановки подов (drain узла, обновление кластера): `minAvailable` или `maxUnavailable` говорят, сколько реплик нужно сохранить. Он не защищает от аварий (падение узла) и не влияет на rolling update Deployment, у которого свои `maxSurge` и `maxUnavailable`. Бюджет, равный числу реплик, блокирует drain узла полностью.

### Частые ошибки в ответе

- «Миграцию можно запустить init-контейнером в каждом поде без оговорок». При нескольких репликах она выполнится несколько раз одновременно; нужны блокировка или отдельный Job.
- «Откатили приложение — откатилась и база». Схема остаётся новой; поэтому она должна работать со старой версией кода.
- «PodDisruptionBudget защищает от падения узла». Он действует только на добровольные остановки.
- «PodDisruptionBudget ограничивает rolling update». Выкаткой управляют `maxSurge` и `maxUnavailable` Deployment.

## Что спросят дальше

- Как выкатывать без потерь запросов и откатывать: [[Деплой в Kubernetes]].
- Что делать при `OOMKilled`, `Pending` и троттлинге: «Отладка в Kubernetes».
- Как Linux ограничивает CPU и память контейнера: [[Контейнеры изнутри]].
- Как приложение должно обрабатывать `TERM`: [[Сигналы в Linux]].
- Как работают StatefulSet и его упорядоченные выкатки: [[Конфигурация и нагрузки в Kubernetes]].
