---
title: Объекты Kubernetes
---

Что такое Kubernetes и из чего он состоит: control plane, цикл согласования, Pod, Deployment с ReplicaSet, labels и selectors, Service с DNS и его типами и namespace. Всё показано прогоном в локальном кластере kind, а не пересказом документации. Конфигурация, init-контейнеры, Job, StatefulSet и Ingress разобраны на второй странице.

Как читать. Поднять песочницу: `make k8s-up`; блок запускается `make k8s-block PAGE="Docker и Kubernetes/Объекты Kubernetes.md" TITLE="<имя>"`, вывод ниже снят именно так. В песочнице один узел kind (Kubernetes v1.37.0), образы `reference-app:1`, `reference-app:2` и `busybox:1.37.0` загружены в узел без реестра, поэтому в манифестах `imagePullPolicy: IfNotPresent`. Демо-приложение отвечает на `GET /` строкой `version=<V> pod=<имя пода>`. Блоки идут серией в namespace `demo-objects`: первый блок серии создаёт его заново, последний удаляет. Имена подов, IP и возраст у вас будут другими.

Рядом: [[Конфигурация и нагрузки в Kubernetes]], [[Контейнеры изнутри]], [[Сигналы в Linux]].

## Вопросы на собеседовании

1. [Что такое Kubernetes и из чего состоит control plane?](#что-такое-kubernetes-и-control-plane)
2. [Что такое Pod и почему поды не создают руками?](#pod)
3. [Как связаны Deployment, ReplicaSet и Pod?](#deployment-replicaset-и-pod)
4. [Что такое цикл согласования и что будет, если удалить под?](#цикл-согласования)
5. [Как Service находит свои поды и зачем нужны labels?](#labels-и-selectors)
6. [Как работает Service и как поды находят друг друга по имени?](#service-и-dns)
7. [Чем отличаются ClusterIP, NodePort, LoadBalancer и headless?](#типы-service)
8. [Что даёт namespace и что он не изолирует?](#namespace)

## Что такое Kubernetes и control plane

Kubernetes работает по декларативной модели. Вы описываете желаемое состояние (объект с `spec`), а контроллеры постоянно сравнивают его с фактическим и приводят второе к первому. Команда «запусти» здесь заменена записью «должно быть две реплики».

Control plane — это компоненты, которые принимают и исполняют желаемое состояние:

- kube-apiserver — единственная точка входа: через него идут `kubectl`, контроллеры и kubelet;
- etcd — хранилище всех данных кластера;
- kube-scheduler — выбирает узел для пода, у которого узла ещё нет;
- kube-controller-manager — один процесс, в котором работают контроллеры (узлов, Job и другие);
- kubelet на каждом узле — запускает контейнеры пода и сообщает о его состоянии;
- kube-proxy на узле — реализует Service (см. ниже).

В kind весь control plane — обычные поды в namespace `kube-system` на единственном узле.

```sh title="control-plane.sh"
kubectl get pods -n kube-system \
  -o custom-columns=NAME:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName
```

```text title="Вывод"
NAME                                              STATUS    NODE
coredns-559f6c778d-56cpz                          Running   reference-control-plane
coredns-559f6c778d-w8qmh                          Running   reference-control-plane
etcd-reference-control-plane                      Running   reference-control-plane
kindnet-hm24q                                     Running   reference-control-plane
kube-apiserver-reference-control-plane            Running   reference-control-plane
kube-controller-manager-reference-control-plane   Running   reference-control-plane
kube-proxy-sln8h                                  Running   reference-control-plane
kube-scheduler-reference-control-plane            Running   reference-control-plane
metrics-server-76558b4bb9-6vblx                   Running   reference-control-plane
```

Имена подов, IP и возраст у вас будут другими. Здесь видны apiserver, etcd, scheduler и controller-manager, kube-proxy и coredns (DNS кластера), а также сетевой плагин kind и metrics-server. Kubelet в списке нет: это процесс на узле, а не под.

### Частые ошибки в ответе

- «Kubernetes запускает контейнеры». Контейнеры запускает kubelet через container runtime; остальные компоненты решают, что и где должно работать.
- «etcd — это база приложения». Это хранилище состояния самого кластера; приложения в него не пишут.
- «Планировщик запускает поды». Scheduler только выбирает узел; запускает под kubelet.

## Pod

Pod — наименьшая единица, которую запускает Kubernetes: один или несколько контейнеров с общей сетью (один IP, общий localhost) и общими томами. Контейнеры пода всегда оказываются на одном узле. Несколько контейнеров в поде нужны для тесно связанных процессов: init-контейнер готовит окружение, sidecar помогает основному (см. «Init-контейнер и sidecar»).

Под смертен и не заменяет сам себя. Если создать под командой `kubectl run` или манифестом `kind: Pod`, его никто не пересоздаст после удаления или падения узла. Поэтому прикладные поды создают через контроллер: Deployment для приложений без состояния, StatefulSet, Job, DaemonSet. Голый под годится для отладки и разовых проверок, как `busybox` ниже.

IP пода меняется при каждом пересоздании. Поэтому к подам ходят не по IP, а через Service.

### Частые ошибки в ответе

- «Под — это контейнер». Под — группа контейнеров с общей сетью и томами; контейнер в нём может быть один.
- «Поды в проде создают `kubectl run`». Без контроллера никто не вернёт под после сбоя.
- «Контейнеры одного пода можно разнести по узлам». Нельзя: под целиком живёт на одном узле.

## Deployment, ReplicaSet и Pod

Deployment не создаёт поды сам. Он создаёт ReplicaSet, а ReplicaSet держит нужное число подов. Зачем промежуточное звено: при обновлении шаблона Deployment создаёт новый ReplicaSet и постепенно переносит в него реплики, уменьшая старый. Документация просит не править ReplicaSet, которые принадлежат Deployment.

Связь записана в `ownerReferences` каждого объекта: у пода владелец — ReplicaSet, у ReplicaSet — Deployment. Сначала создадим namespace и Deployment на две реплики.

```sh title="objects-up.sh"
kubectl delete namespace demo-objects --ignore-not-found --wait --timeout=120s
kubectl create namespace demo-objects
kubectl apply -n demo-objects -f - <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 2
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
        - name: web
          image: reference-app:1
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8080
          readinessProbe:
            httpGet:
              path: /ready
              port: 8080
YAML
kubectl rollout status deployment/web -n demo-objects --timeout=90s
kubectl get deploy,rs,pods -n demo-objects
```

```text title="Вывод"
namespace/demo-objects created
deployment.apps/web created
Waiting for deployment "web" rollout to finish: 0 of 2 updated replicas are available...
Waiting for deployment "web" rollout to finish: 1 of 2 updated replicas are available...
deployment "web" successfully rolled out
NAME                  READY   UP-TO-DATE   AVAILABLE   AGE
deployment.apps/web   2/2     2            2           2s

NAME                             DESIRED   CURRENT   READY   AGE
replicaset.apps/web-64549d9df5   2         2         2       2s

NAME                       READY   STATUS    RESTARTS   AGE
pod/web-64549d9df5-gb4lp   1/1     Running   0          2s
pod/web-64549d9df5-mh48m   1/1     Running   0          2s
```

Имена подов, IP и возраст у вас будут другими. Имя ReplicaSet — имя Deployment плюс хэш шаблона пода, имя пода — имя ReplicaSet плюс случайный суффикс. Теперь прочитаем владельцев из самих объектов.

```sh title="owners.sh"
pod=$(kubectl get pods -n demo-objects -l app=web -o jsonpath='{.items[0].metadata.name}')
rs=$(kubectl get pod "$pod" -n demo-objects \
  -o jsonpath='{.metadata.ownerReferences[0].name}')
echo "pod $pod owner: $(kubectl get pod "$pod" -n demo-objects \
  -o jsonpath='{.metadata.ownerReferences[0].kind}/{.metadata.ownerReferences[0].name}')"
echo "rs  $rs owner: $(kubectl get rs "$rs" -n demo-objects \
  -o jsonpath='{.metadata.ownerReferences[0].kind}/{.metadata.ownerReferences[0].name}')"
```

```text title="Вывод"
pod web-64549d9df5-gb4lp owner: ReplicaSet/web-64549d9df5
rs  web-64549d9df5 owner: Deployment/web
```

Блоки идут серией: этот блок выполняется после блока `objects-up.sh`.

### Частые ошибки в ответе

- «Deployment управляет подами напрямую». Подами управляет ReplicaSet, Deployment управляет ReplicaSet.
- «Реплик меньше, поэтому поправлю ReplicaSet вручную». Deployment вернёт всё как в его шаблоне.

## Цикл согласования

Контроллер ReplicaSet смотрит на число подов с подходящими labels и сравнивает его с `replicas`. Если подов меньше, он создаёт недостающие, если больше — удаляет лишние. Этот же шаблон «сравнить желаемое с фактическим и исправить» повторяют все контроллеры. Удалим под вручную.

```sh title="self-heal.sh"
victim=$(kubectl get pods -n demo-objects -l app=web \
  -o jsonpath='{.items[0].metadata.name}')
echo "deleting $victim"
kubectl delete pod "$victim" -n demo-objects --wait=true
kubectl wait deployment/web -n demo-objects --for=jsonpath='{.status.readyReplicas}'=2 \
  --timeout=60s
kubectl get pods -n demo-objects -l app=web \
  -o custom-columns=NAME:.metadata.name,STATUS:.status.phase,\
CREATED:.metadata.creationTimestamp
```

```text title="Вывод"
deleting web-64549d9df5-gb4lp
pod "web-64549d9df5-gb4lp" deleted from demo-objects namespace
deployment.apps/web condition met
NAME                   STATUS    CREATED
web-64549d9df5-2vrgt   Running   2026-11-21T11:05:27Z
web-64549d9df5-mh48m   Running   2026-11-21T11:05:23Z
```

Блок выполняется после блока `objects-up.sh`. Удалённого пода в списке нет, а рядом с уцелевшим стоит новый с другим суффиксом: ReplicaSet увидел одну реплику вместо двух и создал вторую.

### Частые ошибки в ответе

- «`kubectl delete pod` вылечит упавший под». Это не лечение: ReplicaSet создаст такой же под из того же шаблона и с той же ошибкой, а причина (код, конфиг, нехватка памяти) останется. Сначала `kubectl logs` и `kubectl describe`.
- «`--force --grace-period=0` ускорит удаление». Он сразу освобождает имя пода в API, не дожидаясь подтверждения от kubelet, что под остановлен. Под может продолжать работать, а для StatefulSet это грозит двумя подами с одной идентичностью и потерей данных. Без полной уверенности в последствиях его не используют.

## Labels и selectors

Label — пара «ключ-значение» на объекте. Selector — условие по labels. На них держится связь объектов: Deployment и ReplicaSet выбирают свои поды, Service — поды для трафика. Других ссылок между ними нет.

```sh title="labels.sh"
kubectl get pods -n demo-objects --show-labels
echo "--- app=web"
kubectl get pods -n demo-objects -l app=web -o name
echo "--- app=other"
kubectl get pods -n demo-objects -l app=other
```

```text title="Вывод"
NAME                   READY   STATUS    RESTARTS   AGE   LABELS
web-64549d9df5-2vrgt   1/1     Running   0          2s    app=web,pod-template-hash=64549d9df5
web-64549d9df5-mh48m   1/1     Running   0          6s    app=web,pod-template-hash=64549d9df5
--- app=web
pod/web-64549d9df5-2vrgt
pod/web-64549d9df5-mh48m
--- app=other
No resources found in demo-objects namespace.
```

Блок выполняется после блока `objects-up.sh`. Метка `pod-template-hash` Deployment добавил сам: так ReplicaSet разных ревизий не путают чужие поды.

Из связи через labels следует хитрость: если сменить label у пода, он выйдет из-под ReplicaSet. ReplicaSet увидит недостачу и создаст замену, а старый под останется жить без владельца.

```sh title="detach.sh"
pod=$(kubectl get pods -n demo-objects -l app=web \
  -o jsonpath='{.items[0].metadata.name}')
kubectl label pod "$pod" -n demo-objects app=detached --overwrite
kubectl wait deployment/web -n demo-objects --for=jsonpath='{.status.readyReplicas}'=2 \
  --timeout=60s
kubectl get pods -n demo-objects \
  -o custom-columns=NAME:.metadata.name,APP:.metadata.labels.app,\
OWNER:.metadata.ownerReferences[0].kind
kubectl delete pod "$pod" -n demo-objects --wait=true
```

```text title="Вывод"
pod/web-64549d9df5-2vrgt labeled
deployment.apps/web condition met
NAME                   APP        OWNER
web-64549d9df5-2bpbf   web        ReplicaSet
web-64549d9df5-2vrgt   detached   <none>
web-64549d9df5-mh48m   web        ReplicaSet
pod "web-64549d9df5-2vrgt" deleted from demo-objects namespace
```

Блок выполняется после блока `self-heal.sh`. Отцепленный под с `app=detached` остался без владельца (колонка `OWNER` пуста), ReplicaSet создал третий под. Так из работающего приложения достают под для отладки, не убивая его. В конце блок удаляет отцепленный под.

### Частые ошибки в ответе

- «Selector Service ссылается на Deployment». Только на labels подов. Опечатка в label даёт Service без подов, и ошибки при этом нет.
- «Labels можно менять без последствий». Смена label у работающего пода выводит его из ReplicaSet и из Service.

## Service и DNS

Под живёт недолго, его IP меняется. Service даёт стабильный адрес и DNS-имя набору подов, выбранных по selector. Список подов хранится в объектах EndpointSlice: Kubernetes обновляет их при каждом изменении набора подов. Адрес Service (ClusterIP) виртуальный: ни на одном интерфейсе его нет, а правила на узле (их ставит kube-proxy) переписывают его на IP одного из подов.

```sh title="service-clusterip.sh"
kubectl apply -n demo-objects -f - <<'YAML'
apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  selector:
    app: web
  ports:
    - port: 80
      targetPort: 8080
YAML
kubectl get svc -n demo-objects
kubectl get endpointslices -n demo-objects
kubectl get pods -n demo-objects -l app=web \
  -o custom-columns=NAME:.metadata.name,IP:.status.podIP
```

```text title="Вывод"
service/web created
NAME   TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)   AGE
web    ClusterIP   10.96.127.125   <none>        80/TCP    0s
NAME        ADDRESSTYPE   PORTS   ENDPOINTS                 AGE
web-4hddl   IPv4          8080    10.244.0.41,10.244.0.39   0s
NAME                   IP
web-64549d9df5-2bpbf   10.244.0.41
web-64549d9df5-mh48m   10.244.0.39
```

Блок выполняется после блока `detach.sh`. IP в колонке `ENDPOINTS` у EndpointSlice совпадают с IP подов ниже: Service нашёл их по label `app=web`. Порт Service (80) и порт контейнера (`targetPort` 8080) могут различаться.

Теперь запустим вспомогательный под `busybox` и обратимся к Service по имени. Внутри namespace достаточно короткого имени `web`: kubelet кладёт в `/etc/resolv.conf` пода список `search`, который дописывает `<namespace>.svc.cluster.local`. Полное имя — `<service>.<namespace>.svc.cluster.local`.

```sh title="service-dns.sh"
kubectl run client -n demo-objects --image=busybox:1.37.0 --restart=Never \
  -- sleep 3600
kubectl wait pod/client -n demo-objects --for=condition=Ready --timeout=60s
echo "--- resolv.conf"
kubectl exec client -n demo-objects -- cat /etc/resolv.conf
echo "--- short name"
kubectl exec client -n demo-objects -- wget -qO- http://web; echo
echo "--- full name"
kubectl exec client -n demo-objects -- \
  wget -qO- http://web.demo-objects.svc.cluster.local; echo
echo "--- 10 requests"
kubectl exec client -n demo-objects -- sh -c \
  'for i in 1 2 3 4 5 6 7 8 9 10; do wget -qO- http://web; echo; done' \
  | awk '{print $2}' | sort | uniq -c
```

```text title="Вывод"
pod/client created
pod/client condition met
--- resolv.conf
search demo-objects.svc.cluster.local svc.cluster.local cluster.local
nameserver 10.96.0.10
options ndots:5
--- short name
version=1 pod=web-64549d9df5-mh48m
--- full name
version=1 pod=web-64549d9df5-mh48m
--- 10 requests
   3 pod=web-64549d9df5-2bpbf
   7 pod=web-64549d9df5-mh48m
```

Блок выполняется после блока `service-clusterip.sh`. Короткое и полное имя дают ответ от Service. Десять запросов разошлись по обоим подам; пропорция случайная, она не обязана быть пять к пяти.

Как именно распределяются запросы. В режиме iptables, который описан в документации, kube-proxy ставит правила, которые по умолчанию выбирают под случайно, и выбор происходит в момент установки соединения. Поэтому правильнее говорить, что Service распределяет соединения, а не HTTP-запросы. Клиент с keep-alive, который держит одно соединение, будет бить в один и тот же под. Каждый вызов `wget` выше открывает новое соединение, оттого ответы разные.

### Частые ошибки в ответе

- «Service балансирует HTTP-запросы». Он работает на уровне соединений (L4) и ничего не знает про HTTP. Запросы по одному долгому соединению, например gRPC, уйдут на один под.
- «ClusterIP — это адрес какого-то пода или процесса». Это виртуальный адрес, на котором ничего не слушает.
- «Service без подов выдаст ошибку при создании». Не выдаст: пустой список endpoint'ов проявится только отказом соединений.
- «Короткое имя работает между namespace». Нет: `web` раскроется в namespace самого пода, из чужого нужно `web.<namespace>` (см. «Namespace»).

## Типы Service

Тип задаёт, откуда сервис доступен:

- ClusterIP (по умолчанию) — только изнутри кластера;
- NodePort — дополнительно открывает порт на каждом узле (по умолчанию из диапазона 30000-32767);
- LoadBalancer — просит у облака внешний балансировщик, поверх NodePort; адрес появляется в `status.loadBalancer`;
- headless (`clusterIP: None`) — без виртуального адреса: DNS отвечает IP самих подов.

В kind нет облака, которое выдало бы балансировщик, поэтому у LoadBalancer колонка `EXTERNAL-IP` остаётся `<pending>`.

```sh title="service-types.sh"
kubectl apply -n demo-objects -f - <<'YAML'
apiVersion: v1
kind: Service
metadata:
  name: web-np
spec:
  type: NodePort
  selector:
    app: web
  ports:
    - port: 80
      targetPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: web-lb
spec:
  type: LoadBalancer
  selector:
    app: web
  ports:
    - port: 80
      targetPort: 8080
YAML
kubectl get svc -n demo-objects
```

```text title="Вывод"
service/web-np created
service/web-lb created
NAME     TYPE           CLUSTER-IP      EXTERNAL-IP   PORT(S)        AGE
web      ClusterIP      10.96.127.125   <none>        80/TCP         3s
web-lb   LoadBalancer   10.96.236.177   <pending>     80:30451/TCP   0s
web-np   NodePort       10.96.33.104    <none>        80:30608/TCP   0s
```

Блок выполняется после блока `service-dns.sh`. У `web-np` порт записан как `80:<порт узла>/TCP`, у `web-lb` в kind `EXTERNAL-IP` равен `<pending>`: в облаке там появился бы адрес балансировщика. Пара `web-np` и `web-lb` — песочница: порт узла kind снаружи Docker Desktop не открыт, поэтому в прогоне мы к ним не обращаемся.

Headless-сервис отличается DNS-ответом. Обычный Service отдаёт один виртуальный IP, headless — адреса всех подов. Клиент сам выбирает среди них, поэтому такой сервис нужен там, где важны конкретные поды, например для StatefulSet.

```sh title="service-headless.sh"
kubectl apply -n demo-objects -f - <<'YAML'
apiVersion: v1
kind: Service
metadata:
  name: web-headless
spec:
  clusterIP: None
  selector:
    app: web
  ports:
    - port: 80
      targetPort: 8080
YAML
kubectl get svc web web-headless -n demo-objects
echo "--- nslookup web"
kubectl exec client -n demo-objects -- sh -c \
  "nslookup web 2>&1 | grep -E '^(Name|Address)'"
echo "--- nslookup web-headless"
kubectl exec client -n demo-objects -- sh -c \
  "nslookup web-headless 2>&1 | grep -E '^(Name|Address)'"
kubectl get pods -n demo-objects -l app=web \
  -o custom-columns=NAME:.metadata.name,IP:.status.podIP
```

```text title="Вывод"
service/web-headless created
NAME           TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)   AGE
web            ClusterIP   10.96.127.125   <none>        80/TCP    4s
web-headless   ClusterIP   None            <none>        80/TCP    0s
--- nslookup web
Address:	10.96.0.10:53
Name:	web.demo-objects.svc.cluster.local
Address: 10.96.127.125
--- nslookup web-headless
Address:	10.96.0.10:53
Name:	web-headless.demo-objects.svc.cluster.local
Address: 10.244.0.39
Name:	web-headless.demo-objects.svc.cluster.local
Address: 10.244.0.41
NAME                   IP
web-64549d9df5-2bpbf   10.244.0.41
web-64549d9df5-mh48m   10.244.0.39
```

Блок выполняется после блока `service-types.sh`. У `web-headless` `CLUSTER-IP` равен `None`. DNS-имя `web` даёт один адрес (ClusterIP), `web-headless` — два адреса, и они совпадают с IP подов в списке ниже.

### Частые ошибки в ответе

- «LoadBalancer работает везде». Он нужен облачный или иной контроллер; без него адрес остаётся `<pending>`.
- «NodePort — это способ опубликовать сервис в проде». Порт открывается на каждом узле, адреса узлов ненадёжны для клиентов; для внешнего трафика берут LoadBalancer или Ingress.
- «Headless-сервис балансирует». Он ничего не балансирует: DNS возвращает адреса, а выбор делает клиент.

## Namespace

Namespace делит кластер на логические области: имена объектов уникальны внутри namespace, через него делят ресурсы кластера между пользователями (квоты). Мы работали в `demo-objects`; `kube-system` — системные компоненты. DNS-имя сервиса содержит namespace, поэтому одноимённые сервисы в разных namespace не конфликтуют, а короткое имя раскрывается только в своём.

```sh title="namespace.sh"
kubectl create namespace demo-objects-other
kubectl run probe -n demo-objects-other --image=busybox:1.37.0 --restart=Never \
  -- sleep 3600
kubectl wait pod/probe -n demo-objects-other --for=condition=Ready --timeout=60s
echo "--- short name from another namespace"
kubectl exec probe -n demo-objects-other -- wget -qO- -T 3 http://web \
  || echo "exit=$?"
echo "--- web.demo-objects"
kubectl exec probe -n demo-objects-other -- wget -qO- http://web.demo-objects; echo
kubectl get ns -o name | grep demo-
```

```text title="Вывод"
namespace/demo-objects-other created
pod/probe created
pod/probe condition met
--- short name from another namespace
wget: bad address 'web'
command terminated with exit code 1
exit=1
--- web.demo-objects
version=1 pod=web-64549d9df5-mh48m
namespace/demo-objects
namespace/demo-objects-other
```

Блок выполняется после блока `service-headless.sh`. Короткое имя `web` в чужом namespace не раскрывается (`bad address`, код выхода 1 показан намеренно), а `web.demo-objects` доходит до сервиса: namespace не изолирует сеть, поды по умолчанию принимают соединения из любого источника, и ограничить это можно только NetworkPolicy при сетевом плагине, который их поддерживает.

Последний блок серии удаляет оба namespace и ждёт их исчезновения. Вместе с namespace удаляются все объекты в нём.

```sh title="objects-down.sh"
kubectl delete namespace demo-objects demo-objects-other --wait --timeout=180s
kubectl get ns -o name | grep demo- || echo "no demo namespaces left"
```

```text title="Вывод"
namespace "demo-objects" deleted
namespace "demo-objects-other" deleted
no demo namespaces left
```

### Частые ошибки в ответе

- «Namespace изолирует поды друг от друга». Он делит имена и ресурсы, но не сеть: нужны NetworkPolicy.
- «Namespace — граница безопасности сама по себе». Он делит имена и ресурсы; сетевой доступ по умолчанию открыт, ограничения задают отдельными объектами.
- «Короткое имя сервиса работает из любого namespace». Нужно `<service>.<namespace>`.

## Что спросят дальше

- Как Deployment обновляет приложение без простоя: стратегия rolling update, `maxSurge`, `maxUnavailable`, откат.
- Что делают readiness-, liveness- и startup-пробы и почему liveness не должна проверять соседние сервисы.
- Чем `requests` отличаются от `limits` и что делает scheduler с `requests`.
- Как под останавливается при удалении: `terminationGracePeriodSeconds`, SIGTERM, `preStop` (связь с [[Сигналы в Linux#Kubernetes при остановке пода]]).
- Как контейнер получает изоляцию на уровне ядра, пока Kubernetes только запускает его (см. [[Контейнеры изнутри]]).
- Как сети и NetworkPolicy связаны с CNI и как kube-proxy работает в режимах iptables и IPVS.
