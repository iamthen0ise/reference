---
title: Конфигурация и нагрузки в Kubernetes
---

Как приложение получает настройки и как запускаются не только веб-сервисы: ConfigMap и Secret (в env и файлом), init-контейнеры и sidecar, Job, StatefulSet, CronJob и DaemonSet, а также Ingress и Gateway API. Продолжение страницы «Объекты Kubernetes»; всё показано прогоном в локальном кластере kind.

Как читать. Поднять песочницу: `make k8s-up`; блок запускается `make k8s-block PAGE="Docker и Kubernetes/Конфигурация и нагрузки в Kubernetes.md" TITLE="<имя>"`, вывод ниже снят именно так. В песочнице один узел kind (Kubernetes v1.37.0), образ `busybox:1.37.0` загружен в узел без реестра, а для тега без `latest` Kubernetes по умолчанию берёт `imagePullPolicy: IfNotPresent`. Блоки идут серией в namespace `demo-workloads`: первый блок создаёт его заново, последний удаляет. Имена подов, IP и возраст у вас будут другими.

Рядом: [[Объекты Kubernetes]], [[Контейнеры изнутри]].

## Вопросы на собеседовании

1. [Чем ConfigMap отличается от Secret и зашифрован ли Secret?](#configmap-и-secret)
2. [Чем init-контейнер отличается от sidecar?](#init-контейнер-и-sidecar)
3. [Когда нужны Job, CronJob, DaemonSet и StatefulSet?](#job-statefulset-cronjob-и-daemonset)
4. [Что делают Ingress и Gateway API?](#ingress-и-gateway-api)

## ConfigMap и Secret

ConfigMap хранит настройки открытым текстом, Secret — чувствительные значения. Оба объекта подключаются к поду двумя способами: переменной окружения или файлом в томе. Лимит ConfigMap — 1 MiB.

```sh title="config-secret.sh"
kubectl delete namespace demo-workloads --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-workloads
kubectl apply -n demo-workloads -f - <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
  GREETING: hello
  app.properties: |
    mode=demo
    retries=3
---
apiVersion: v1
kind: Secret
metadata:
  name: db
type: Opaque
stringData:
  user: app
  password: s3cret
---
apiVersion: v1
kind: Pod
metadata:
  name: config-check
spec:
  restartPolicy: Never
  containers:
    - name: check
      image: busybox:1.37.0
      command:
        - sh
        - -c
        - |
          echo "env GREETING=$GREETING"
          echo "env DB_PASSWORD=$DB_PASSWORD"
          echo "--- /etc/cfg"
          ls /etc/cfg
          cat /etc/cfg/app.properties
          echo "--- /etc/secret"
          ls /etc/secret
          echo "file password=$(cat /etc/secret/password)"
      env:
        - name: GREETING
          valueFrom:
            configMapKeyRef:
              name: app-config
              key: GREETING
        - name: DB_PASSWORD
          valueFrom:
            secretKeyRef:
              name: db
              key: password
      volumeMounts:
        - name: cfg
          mountPath: /etc/cfg
        - name: secret
          mountPath: /etc/secret
          readOnly: true
  volumes:
    - name: cfg
      configMap:
        name: app-config
    - name: secret
      secret:
        secretName: db
YAML
kubectl wait pod/config-check -n demo-workloads \
  --for=jsonpath='{.status.phase}'=Succeeded --timeout=60s
kubectl logs config-check -n demo-workloads
```

```text title="Вывод"
namespace/demo-workloads created
configmap/app-config created
secret/db created
pod/config-check created
pod/config-check condition met
env GREETING=hello
env DB_PASSWORD=s3cret
--- /etc/cfg
GREETING
app.properties
mode=demo
retries=3
--- /etc/secret
password
user
file password=s3cret
```

Каждый ключ ConfigMap и Secret стал файлом в каталоге тома, а `GREETING` и `DB_PASSWORD` попали и в окружение. Обратите внимание: ключ `app.properties` превратился в файл целиком, с несколькими строками.

Чем способы отличаются при изменении. Файлы из тома kubelet обновляет сам, с задержкой до периода синхронизации плюс кэш, а переменные окружения не обновляются, пока под не перезапущен. Файл, смонтированный через `subPath`, обновлений тоже не получает. Поэтому приложение, которое читает конфиг один раз при старте, всё равно требует перезапуска.

Теперь посмотрим, как хранится Secret.

```sh title="secret-read.sh"
echo "--- data in the API object"
kubectl get secret db -n demo-workloads -o jsonpath='{.data}{"\n"}'
echo "--- decoded"
kubectl get secret db -n demo-workloads -o jsonpath='{.data.password}' | base64 -d
echo
```

```text title="Вывод"
--- data in the API object
{"password":"czNjcmV0","user":"YXBw"}
--- decoded
s3cret
```

Блок выполняется после блока `config-secret.sh`. Значение в объекте лежит в base64, а `base64 -d` возвращает исходный текст: это кодировка, а не шифрование. Секрет по умолчанию хранится в etcd в таком виде; шифрование на диске (encryption at rest) включают отдельно, а доступ к значению ограничивают через RBAC. Любой, кто может создавать поды в namespace, может подключить Secret и прочитать его.

### Частые ошибки в ответе

- «Secret зашифрован». По умолчанию это base64 в etcd. Нужны encryption at rest, минимальные права RBAC и при необходимости внешнее хранилище секретов.
- «Обновил ConfigMap, и приложение подхватит». Только файлы из тома, и приложение должно их перечитать; переменные окружения требуют перезапуска пода.
- «ConfigMap можно использовать для паролей». Для паролей Secret с ограниченным доступом; в ConfigMap лежит только несекретное.
- «Пароль можно положить в `ENV` Dockerfile». Он окажется в слоях образа и в `docker history`.

## Init-контейнер и sidecar

Init-контейнеры выполняются до основных, по одному и по порядку, и должны завершиться успешно: пока этого нет, основные контейнеры не стартуют. Через них готовят данные, ждут зависимость, применяют миграцию.

Sidecar — контейнер, который работает рядом с основным всё время жизни пода: шиппер логов, прокси. Он задаётся как init-контейнер с `restartPolicy: Always`. Такой контейнер стартует до основных, но не блокирует запуск (он «запущен», а не «завершён»), останавливается после основных контейнеров. Sidecar как отдельная возможность стабилен с Kubernetes v1.33.

```sh title="init-sidecar.sh"
kubectl apply -n demo-workloads -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: with-init
spec:
  volumes:
    - name: work
      emptyDir: {}
  initContainers:
    - name: prepare
      image: busybox:1.37.0
      command: ["sh", "-c", "echo prepared-by-init > /work/state.txt"]
      volumeMounts:
        - name: work
          mountPath: /work
    - name: heartbeat
      image: busybox:1.37.0
      restartPolicy: Always
      command:
        - sh
        - -c
        - 'while true; do echo "sidecar alive" >> /work/hb.log; sleep 1; done'
      volumeMounts:
        - name: work
          mountPath: /work
  containers:
    - name: main
      image: busybox:1.37.0
      command:
        - sh
        - -c
        - 'cat /work/state.txt; sleep 3; tail -n 1 /work/hb.log; sleep 3600'
      volumeMounts:
        - name: work
          mountPath: /work
YAML
kubectl wait pod/with-init -n demo-workloads --for=condition=Ready --timeout=60s
for i in $(seq 1 20); do
  kubectl logs with-init -n demo-workloads -c main | grep -q 'sidecar alive' && break
  sleep 1
done
kubectl get pod with-init -n demo-workloads
echo "--- init containers"
fmt='{range .status.initContainerStatuses[*]}{.name} exit='
fmt="$fmt{.state.terminated.exitCode} up={.state.running.startedAt}"
kubectl get pod with-init -n demo-workloads -o jsonpath="$fmt"'{"\n"}{end}'
echo "--- main log"
kubectl logs with-init -n demo-workloads -c main
```

```text title="Вывод"
pod/with-init created
pod/with-init condition met
NAME        READY   STATUS    RESTARTS   AGE
with-init   2/2     Running   0          4s
--- init containers
prepare exit=0 up=
heartbeat exit= up=2026-11-21T11:08:31Z
--- main log
prepared-by-init
sidecar alive
```

Блок выполняется после блока `secret-read.sh`. `READY 2/2` — это sidecar и основной контейнер: завершённый init-контейнер `prepare` в счёт не идёт. У `prepare` код выхода 0 и пустое `up` (контейнер не работает), у `heartbeat` — наоборот: `up` — время старта, кода выхода нет, контейнер работает. Основной контейнер прочитал файл, который записал init, и строку, которую дописывает sidecar: у них общий том `emptyDir`.

### Частые ошибки в ответе

- «Init-контейнер и sidecar — одно и то же». Init завершается до старта приложения, sidecar живёт вместе с ним.
- «Sidecar — это второй контейнер в `containers`». Так тоже можно, но тогда у него нет порядка запуска и остановки; настоящий sidecar объявляется в `initContainers` с `restartPolicy: Always`.
- «Init-контейнер — мелочь, его можно не проверять». Пока init-контейнер не завершится, основные контейнеры не стартуют, и зависший init держит под в состоянии `Init`.

## Job, StatefulSet, CronJob и DaemonSet

Job запускает под и следит, чтобы он завершился успешно нужное число раз (`completions`). В отличие от Deployment, завершившийся под здесь норма, а не повод для пересоздания.

```sh title="job.sh"
kubectl apply -n demo-workloads -f - <<'YAML'
apiVersion: batch/v1
kind: Job
metadata:
  name: once
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: once
          image: busybox:1.37.0
          command: ["sh", "-c", "echo job done"]
YAML
kubectl wait job/once -n demo-workloads --for=condition=complete --timeout=60s
kubectl get job once -n demo-workloads
kubectl logs job/once -n demo-workloads
```

```text title="Вывод"
job.batch/once created
job.batch/once condition met
NAME   STATUS     COMPLETIONS   DURATION   AGE
once   Complete   1/1           3s         3s
job done
```

Блок выполняется после блока `init-sidecar.sh`. `COMPLETIONS 1/1` означает, что один из одного нужных запусков завершился успешно.

StatefulSet нужен приложениям с состоянием. Поды получают стабильные имена `<имя>-0`, `<имя>-1` и поднимаются по порядку; у каждого свой том из `volumeClaimTemplates`; имя пода и его том переживают пересоздание. Для стабильных DNS-имён StatefulSet нужен headless-сервис, который указан в `serviceName`.

```sh title="statefulset.sh"
kubectl apply -n demo-workloads -f - <<'YAML'
apiVersion: v1
kind: Service
metadata:
  name: db
spec:
  clusterIP: None
  selector:
    app: db
  ports:
    - port: 5432
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
spec:
  serviceName: db
  replicas: 2
  selector:
    matchLabels:
      app: db
  template:
    metadata:
      labels:
        app: db
    spec:
      containers:
        - name: db
          image: busybox:1.37.0
          command: ["sleep", "3600"]
          volumeMounts:
            - name: data
              mountPath: /data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 10Mi
YAML
kubectl rollout status statefulset/db -n demo-workloads --timeout=120s
kubectl get pods -n demo-workloads -l app=db
kubectl get pvc -n demo-workloads -o custom-columns=\
NAME:.metadata.name,STATUS:.status.phase,SIZE:.status.capacity.storage,\
CLASS:.spec.storageClassName
kubectl delete pod db-0 -n demo-workloads --wait=true
kubectl rollout status statefulset/db -n demo-workloads --timeout=120s
echo "--- after deleting db-0"
kubectl get pods -n demo-workloads -l app=db \
  -o custom-columns=NAME:.metadata.name,STATUS:.status.phase,\
CREATED:.metadata.creationTimestamp
```

```text title="Вывод"
service/db created
statefulset.apps/db created
Waiting for 2 pods to be ready...
Waiting for 1 pods to be ready...
Waiting for 1 pods to be ready...
partitioned roll out complete: 2 new pods have been updated...
NAME   READY   STATUS    RESTARTS   AGE
db-0   1/1     Running   0          8s
db-1   1/1     Running   0          3s
NAME        STATUS   SIZE   CLASS
data-db-0   Bound    10Mi   standard
data-db-1   Bound    10Mi   standard
pod "db-0" deleted from demo-workloads namespace
Waiting for 1 pods to be ready...
partitioned roll out complete: 2 new pods have been updated...
--- after deleting db-0
NAME   STATUS    CREATED
db-0   Running   2026-11-21T11:09:18Z
db-1   Running   2026-11-21T11:08:44Z
```

Блок выполняется после блока `job.sh`. Поды называются `db-0` и `db-1` (не `db-<хэш>`), у каждого свой PVC `data-db-0` и `data-db-1`. После удаления `db-0` под вернулся под тем же именем, а PVC `data-db-0` на месте. Класс хранилища в kind — локальный провайзер на диске узла.

Остальные два объекта кратко:

- CronJob создаёт Job по расписанию в формате cron (бэкапы, отчёты); каждый запуск — отдельный Job;
- DaemonSet запускает по одному поду на каждом узле (или на выбранных): сборщик логов, агент мониторинга. В kind один узел, и в этой песочнице он дал бы один под.

Последний блок серии удаляет namespace и ждёт его исчезновения. Вместе с ним удаляются все объекты, включая PVC StatefulSet.

```sh title="workloads-down.sh"
kubectl delete namespace demo-workloads --wait --timeout=180s
kubectl get ns -o name | grep demo- || echo "no demo namespaces left"
```

```text title="Вывод"
namespace "demo-workloads" deleted
no demo namespaces left
```

### Частые ошибки в ответе

- «Для базы достаточно Deployment с одной репликой». Без стабильной идентичности и тома по шаблону это работает хуже, чем StatefulSet; но StatefulSet сам не делает базу отказоустойчивой, репликацию настраивает приложение.
- «При удалении StatefulSet удаляется и данные». Тома не удаляются: документация делает это ради сохранности данных.
- «Job можно держать в Deployment». У Deployment завершившийся под будет пересоздан.
- «CronJob — это просто cron внутри пода». Это объект, который создаёт Job.

## Ingress и Gateway API

Service с типом LoadBalancer открывает наружу один сервис. Для HTTP нужно больше: маршрутизация по хосту и пути, TLS, один внешний адрес на много сервисов. Это делают Ingress и Gateway API. В песочницу контроллер не ставится, поэтому только текстом.

Ingress — объект с правилами «хост и путь ведут в сервис». Сам по себе он ничего не делает: нужен Ingress controller, который читает объекты и настраивает прокси. Документация: Ingress стабилен с v1.19, но его API заморожен, развитие остановлено, а проект Kubernetes рекомендует Gateway.

Gateway API — семейство объектов, которое разводит роли: GatewayClass (какой контроллер), Gateway (точка входа, слушатель), HTTPRoute (правила маршрутизации). Это надстройка, не встроенная в Kubernetes: CRD и реализацию ставят отдельно. Стабильных видов четыре: GatewayClass, Gateway, HTTPRoute, GRPCRoute.

Про ingress-nginx. Блог kubernetes.io объявил 11 ноября 2025 года, что поддержка ingress-nginx (проект Kubernetes SIG Network) будет прекращена: бессрочная поддержка до марта 2026, затем ни релизов, ни исправлений ошибок и уязвимостей. Существующие установки продолжат работать. Заявление Steering Committee от 29 января 2026 года повторяет: в марте 2026 года Kubernetes выводит Ingress NGINX из поддержки.

### Частые ошибки в ответе

- «Создал Ingress, и всё работает». Без Ingress controller объект не имеет эффекта.
- «Ingress удаляют из Kubernetes». Нет: у Ingress статус GA и планов его удалять нет; его API заморожен и не развивается.

## Что спросят дальше

- Как включить шифрование Secret в etcd и чем его заменяют внешние хранилища секретов.
- Как перезапустить Deployment после смены ConfigMap и зачем хэш конфига в аннотации шаблона.
- Как StatefulSet обновляется (`partition`) и что будет с PVC при уменьшении числа реплик.
- Как ограничить параллельные запуски CronJob (`concurrencyPolicy`) и что делают `backoffLimit` и `ttlSecondsAfterFinished` у Job.
- Чем Gateway API отличается от Ingress по ролям и как перенести правила Ingress.
