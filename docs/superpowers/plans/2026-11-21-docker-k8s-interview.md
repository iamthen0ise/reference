# Docker и Kubernetes к собеседованию — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить рубрику меню верхнего уровня «Docker и Kubernetes» из пяти страниц и указателя. Каждая демонстрация прогоняется: Docker — на Docker Desktop, Kubernetes — в локальном кластере kind, через инструмент в репозитории.

**Architecture:** Страницы — заметки Obsidian в `Docker и Kubernetes/`, пункты меню — в `sidebar` в `.site/astro.config.mjs`. Демонстрации живут только в `.md`, у каждой уникальный на странице `title`. Скрипт `.site/scripts/k8s-box.sh` поднимает кластер kind со своим kubeconfig, грузит в него образы демо-приложения и прогоняет блок страницы на хосте через `bash` во временном каталоге. Перед прогоном он отказывается работать с чужим `kubectl`-контекстом и нелокальным `docker`. Вывод вставляется в страницу вручную.

**Tech Stack:** Obsidian markdown, Astro Starlight, Docker Desktop 29.5 (ядро linuxkit 6.12, cgroups v2), kind 0.33.0, kubectl 1.36.1, metrics-server, bash, Python 3.13 (демо-приложение, только стандартная библиотека).

**Spec:** `docs/superpowers/specs/2026-11-21-docker-k8s-interview-design.md`. Исполнитель читает его целиком: состав каждой страницы перечислен там и здесь не повторяется.

## Global Constraints

- Папка `Docker и Kubernetes/` в корне vault. Группа меню `{ label: 'Docker и Kubernetes', items: pages('Docker и Kubernetes', [...]) }` идёт в `sidebar` сразу после группы `'Linux'`. Итоговый порядок: `['Docker и Kubernetes', 'Образы Docker', 'Docker на практике', 'Объекты Kubernetes', 'Деплой в Kubernetes', 'Отладка в Kubernetes']`. Задача 2 создаёт группу, каждая задача-страница вставляет свою страницу на её место в этом порядке.
- Имена файлов ровно такие: `Docker и Kubernetes.md`, `Образы Docker.md`, `Docker на практике.md`, `Объекты Kubernetes.md`, `Деплой в Kubernetes.md`, `Отладка в Kubernetes.md` (свободны во vault на 2026-11-21). Frontmatter `title` совпадает с именем файла. Если страница делится на две (лимит 500 строк с выводом), имена частей исполнитель выбирает сам, проверив уникальность, и ставит обе подряд на место исходной.
- Формат страницы как у `Linux/Сигналы в Linux.md`: frontmatter `title`; абзац «о чём»; строка `Рядом:`; `## Вопросы на собеседовании` со ссылкой каждого вопроса на свой раздел; разделы `##` с механизмом, демонстрацией и `### Частые ошибки в ответе`; последним — `## Что спросят дальше`. У указателя схема своя, см. задачу 7.
- Запускаемая демонстрация — блок ```` ```sh title="<имя>.sh" ````, `title` уникален на странице. Сразу под ним — вывод в ```` ```text title="Вывод" ````. Манифесты — внутри блока через `kubectl apply -f - <<'YAML'`, Dockerfile — через `cat > Dockerfile <<'EOF'` (блок выполняется во временном каталоге). Фрагменты без `title` (пример YAML, Dockerfile для чтения) не запускаются и подписаны «пример, не прогоняется».
- Каждый вывод получен через `make k8s-block`. Изменчивые значения (хэши в именах подов, IP, `AGE`, время, ID образов) помечены строкой под блоком: «имена подов, IP и возраст у вас будут другими».
- Изоляция состояния:
  - сценарий Kubernetes — один блок или серия блоков в своём namespace `demo-<слаг>`; первый блок серии удаляет такой namespace, если он есть, и создаёт заново, последний удаляет его с `--wait`; блоки серии подписаны «после блока `<title>`»;
  - Docker-блок называет всё, что создаёт (контейнеры, образы, сети, тома, проект compose), с префиксом `ref-` и удаляет это в конце блока; `docker builder prune` и `docker system prune` не вызывать.
- Ожидание в блоках — по условию с пределом (`kubectl wait --for=… --timeout=…`, `kubectl rollout status --timeout=…` или цикл опроса с дедлайном), а не голым `sleep N`.
- Строка вывода — не шире ~90 символов. Колонки задаются явно (`-o custom-columns=…`, `--format` у `docker`), из `describe` берутся нужные строки (`grep -A`, `sed -n`), обрезанное помечается `…`.
- Особенности песочницы подписываются там, где они видны в выводе: один узел kind, образы загружены без реестра (`imagePullPolicy: IfNotPresent`), `EXTERNAL-IP <pending>` у `LoadBalancer`, metrics-server с `--kubelet-insecure-tls`, память узла — это память ВМ Docker Desktop.
- Заголовки разделов — без обратных кавычек, двоеточий и «…».
- Каждое неочевидное утверждение сверено с kubernetes.io/docs или docs.docker.com (поведение, зависящее от версии, подписано версией). Не сверилось — на страницу не попадает. Список сверенных утверждений с источником исполнитель кладёт в отчёт задачи, не на страницу.
- Каждое утверждение и каждый вредный совет из «Анти-целей» спеки, относящиеся к теме страницы, опровергнуты или снабжены предупреждением в «Частых ошибках в ответе» этой страницы.
- Разборы из «Контейнеры изнутри» и «Сигналы в Linux» не повторяются — только ссылка `[[Страница#Раздел]]`.
- Ни одной команды `kubectl` вне `make k8s-block`/`make k8s-up` в ходе работы над страницами: контекст `kubectl` по умолчанию у автора — продовый EKS.
- Правила для исполнителя: `~/.agents/rules/shell-core.md` (скрипт, тест, sh-блоки), `docker-core.md` (Dockerfile демо-приложения и Dockerfile на страницах), `k8s-deploy.md` (ориентир по пробам и ресурсам), `python-core.md` (демо-приложение).
- Одна задача — один коммит в `main` с подписью `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, файлы в `git add` по именам. `.obsidian/` не коммитить. Пушит пользователь.

## Review Focus

1. **Блок уходит в продовый кластер.** У автора `~/.kube/config` смотрит на прод, может быть задан `KUBECONFIG`, блок может передать `--context`. Задача 1: тесты `env-kubeconfig-ignored`, `no-kubeconfig`, `wrong-context`, `override`.
2. **`docker` смотрит не на Docker Desktop** (`DOCKER_HOST`, другой `docker context`), и `kind`, `docker run --memory`, удаление образов идут на чужой хост. Задача 1: тесты `docker-host`, `docker-context`.
3. **Блок зависает** (ждёт под, который никогда не станет Ready), и вместе с ним висит `make k8s-block`; дети блока (`kubectl port-forward`, `sleep`) переживают его. Задача 1: тест `timeout` с проверкой, что дочерних процессов не осталось.
4. **Состояние утекает между блоками:** namespace, контейнеры, сети, тома от прошлого прогона меняют вывод следующего. Задачи 2–6: правило изоляции из Global Constraints и проверка в общем шаге «Убрать за собой».
5. **Вывод зависит от песочницы, а читатель примет его за общий случай:** один узел, локальные образы, metrics-server без TLS, `<pending>` у LoadBalancer, первая сборка Docker из кэша. Задачи 2–6: подпись по Global Constraints; задача 5: первая сборка в демонстрации кэша идёт с `--no-cache`.

---

### Общие шаги каждой задачи-страницы

Задачи 2–6 идут по одной схеме, в самих задачах указано только своё.

- [ ] **Поднять песочницу** (задачи 2–4; задачам 5–6 кластер не нужен): `make k8s-up`. Последняя строка — `ready: kind-reference (Kubernetes v…, kubectl v…)`.
- [ ] **Написать страницу** по спеке (раздел «Состав», пункт с номером страницы) и по списку «Обязательно на странице».
- [ ] **Прогон:** каждый блок — `make k8s-block PAGE="Docker и Kubernetes/<страница>.md" TITLE="<имя>"`, вывод — в блок «Вывод». Код выхода 0, если демонстрация не показывает ненулевой код намеренно (тогда это сказано в тексте).
- [ ] **Меню:** вставить страницу в группу «Docker и Kubernetes» на её место.
- [ ] **Сборка:** `make check` — выход 0.
- [ ] **Телефон:** `make build && make check-browser`, новой страницы нет среди нарушений.
- [ ] **Убрать за собой:** временная страница вне репозитория с блоком `docker ps -aq --filter name=ref-; docker network ls -q --filter name=ref-; docker volume ls -q --filter name=ref-` (в задачах 2–4 ещё `kubectl get ns -o name`) прогнать через `bash .site/scripts/k8s-box.sh block <страница> <title>`: namespace `demo-*` нет, списки Docker пусты. Затем `make k8s-down` (задачи 2–4).
- [ ] **Коммит:** `git add "Docker и Kubernetes/<страница>.md" .site/astro.config.mjs && git commit`.

---

### Task 1: Песочница kind и демо-приложение

Риск: высокий — скрипт запускает `kubectl` и `docker` с записью на машине, где контекст по умолчанию — продовый кластер.

**Files:**
- Create: `.site/k8s-box/app/app.py`, `.site/k8s-box/app/Dockerfile`
- Create: `.site/scripts/k8s-box.sh`, `.site/scripts/k8s-box.test.sh`
- Modify: `Makefile` — цели `k8s-up`, `k8s-block`, `k8s-down`, `k8s-test`, все в `.PHONY`; комментарий над ними в стиле соседних
- Modify: `.gitignore` — строка `.site/k8s-box/kubeconfig`

**Interfaces:**
- Produces (задачи 2–6 пользуются только этим):
  - Переменные: `K8S_BOX_CLUSTER` (по умолчанию `reference`), `K8S_BOX_KUBECONFIG` (по умолчанию `<repo>/.site/k8s-box/kubeconfig`), `K8S_BOX_TIMEOUT` (секунды, по умолчанию `300`).
  - `make k8s-up` → `k8s-box.sh up`:
    - проверка docker (ниже); `kind delete cluster` с тем же именем, если есть; `kind create cluster --name <c> --image kindest/node:<версия>@sha256:<digest> --kubeconfig <kubeconfig> --wait 120s` — версия и digest образа узла взяты из release notes kind v0.33.0 для версии по умолчанию и записаны константой в скрипте;
    - отказ (выход 1, `kubectl version skew: client vA.B, server vX.Y`), если минорные версии `kubectl` и сервера расходятся больше чем на одну;
    - metrics-server: манифест `components.yaml` закреплённого релиза (константа в скрипте), к аргументам контейнера добавлен `--kubelet-insecure-tls`, ожидание `rollout status -n kube-system deploy/metrics-server`;
    - образы `reference-app:1` и `reference-app:2` из `.site/k8s-box/app` (`--build-arg VERSION=1|2`) и `busybox:<закреплённый тег>` загружены в узел через `kind load docker-image`;
    - последняя строка `ready: kind-<c> (Kubernetes v<сервер>, kubectl v<клиент>)`.
  - `make k8s-block PAGE="<путь .md>" TITLE="<имя>"` → `k8s-box.sh block PAGE TITLE`. Порядок:
    1. найти ровно один блок ```` ```sh title="<имя>" ```` (разбор как `extract_block` в `linux-box.sh`, только язык `sh`); нет — выход 1, `block not found: <имя>`; больше одного — выход 1, `block not unique: <имя>`;
    2. тело содержит `--context`, `--kubeconfig` или `KUBECONFIG=` — выход 1, `block overrides kubeconfig: <имя>`;
    3. проверка docker: `DOCKER_HOST` задан — выход 1, `docker is not local: DOCKER_HOST=<значение>`; `docker context show` упал или вернул не `desktop-linux` и не `default` — выход 1, `docker is not local: context <имя>`;
    4. если в теле есть слово `kubectl` (не часть другого слова): файла kubeconfig нет — выход 1, `no sandbox kubeconfig; run make k8s-up`; `kubectl config current-context --kubeconfig <файл>` не равен `kind-<c>` — выход 1, `wrong kubectl context: <контекст>`; кластера нет в `kind get clusters` — выход 1, `not running: <c>; run make k8s-up`;
    5. выполнить тело через `bash` из `PATH` с `KUBECONFIG=<sandbox kubeconfig>` (всегда, даже для Docker-блоков; значение вызывающего игнорируется), stdin `/dev/null`, cwd — новый `mktemp -d`, который удаляется после блока при любом исходе;
    6. сторож времени без coreutils `timeout` (на macOS его нет): по истечении `K8S_BOX_TIMEOUT` блок и все его потомки получают `TERM`, через 5 с — `KILL`; выход 124 и `block timed out: <имя>`. Иначе код выхода — код блока.
  - `make k8s-down` → `kind delete cluster --name <c>` и удаление файла kubeconfig; кластера нет — выход 0.
  - `make k8s-test` → `bash .site/scripts/k8s-box.test.sh`. Кластер и сеть не нужны, нужен только запущенный Docker Desktop. В `make check` не входит.
  - Демо-приложение (`app.py`, только стандартная библиотека, порт 8080):
    - `GET /healthz` → 200 `ok`; `GET /ready` и `GET /` → 503 `warming up`, пока с запуска не прошло `READY_DELAY_SECONDS` (по умолчанию 0); после — `/ready` 200 `ready`, `/` 200 `version=<APP_VERSION> pod=<hostname>`; `NOT_READY=1` — `/ready` всегда 503;
    - `CRASH_ON_START=1` → в stderr `fatal: CRASH_ON_START=1`, выход 1 сразу;
    - `ALLOC_MB=N` → при старте выделить N МиБ и записать в каждую страницу (иначе RSS не растёт), в stdout `allocated N MiB`;
    - `CPU_BURN=1` → фоновый поток в бесконечном цикле;
    - `TERM`: по умолчанию в stdout `got TERM, shutting down`, сервер останавливается, выход 0; `IGNORE_TERM=1` — `TERM` игнорируется;
    - каждый запрос, кроме `/healthz` и `/ready`, пишется в stdout строкой `<метод> <путь> <код>`; `APP_VERSION` по умолчанию из build-arg `VERSION`.
  - `Dockerfile` приложения: база `python:3.13-slim` по digest, не root, exec-форма `CMD ["python", "-u", "app.py"]`, `EXPOSE 8080`.

- [ ] **Шаг 1: тест до кода.** `k8s-box.test.sh` (`set -uo pipefail`, временный каталог и `trap`) пишет страницы-фикстуры и задаёт `K8S_BOX_CLUSTER=reference-test-$$`, `K8S_BOX_KUBECONFIG=$tmp/kubeconfig`. Фейковые kubeconfig — минимальный YAML с одним контекстом (`prod` или `kind-reference-test-$$`) и сервером `https://127.0.0.1:1`. Проверки (во всех отказах stdout не содержит маркер `RAN`, который печатает блок):
  - `not-found` — код 1, `block not found: nope.sh`;
  - `not-unique` — два `sh`-блока с `title="dup"` → код 1, `block not unique: dup`;
  - `sh-runs` — блок `echo hi` → stdout `hi`, код 0;
  - `exit-code` — блок `exit 3` → код 3;
  - `crlf` — страница с `\r\n` находится и выполняется;
  - `tmp-cwd` — блок `pwd` → путь не равен корню репозитория, после прогона каталога нет;
  - `stdin-null` — блок `cat; echo done` с `K8S_BOX_TIMEOUT=5` → `done`, код 0;
  - `timeout` — `K8S_BOX_TIMEOUT=2`, блок `sleep 31 & sleep 31` → код 124, `block timed out`; через 6 с `pgrep -f "sleep 31"` пусто;
  - `override` — блок `echo RAN; kubectl get pods --context prod` → код 1, `block overrides kubeconfig`;
  - `docker-host` — `DOCKER_HOST=tcp://192.0.2.1:2375`, блок `echo RAN` → код 1, `docker is not local`;
  - `docker-context` — `DOCKER_CONTEXT=reference-test-missing`, блок `echo RAN` → код 1, `docker is not local`;
  - `no-kubeconfig` — файла нет, блок `echo RAN; kubectl get pods` → код 1, `no sandbox kubeconfig`;
  - `wrong-context` — kubeconfig с контекстом `prod`, тот же блок → код 1, `wrong kubectl context: prod`;
  - `not-running` — kubeconfig с контекстом `kind-reference-test-$$`, кластера нет → код 1, `not running: reference-test-$$`;
  - `env-kubeconfig-ignored` — вызывающий задаёт `KUBECONFIG=$tmp/prod.yaml`, блок `echo "$KUBECONFIG"` → stdout равен `$tmp/kubeconfig`;
  - `down-absent` — `down` без кластера → код 0.
- [ ] **Шаг 2:** `make k8s-test` падает: скрипта нет.
- [ ] **Шаг 3:** написать `app.py`, `Dockerfile`, `k8s-box.sh` (bash, `set -euo pipefail`, подкоманды `up|block|down`), цели `Makefile`, строку `.gitignore`.
- [ ] **Шаг 4:** `make k8s-test` — все 16 проверок проходят, второй прогон подряд тоже.
- [ ] **Шаг 5: живой прогон.** `make k8s-up` → строка `ready: …`, версии сервера и клиента в пределах ±1 минорной. Временная страница вне репозитория с блоком: namespace `demo-smoke`, Deployment `reference-app:1` с readiness на `/ready`, `kubectl wait --for=condition=Available`, `kubectl run` с `busybox` → `wget -qO- http://<svc>:8080/` печатает `version=1 pod=…`; `kubectl top pod` после ожидания метрик печатает строку; namespace удалён. Затем `make k8s-down`, `kind get clusters` не содержит `reference`.
- [ ] **Шаг 6: коммит** `app.py`, `Dockerfile`, скрипт, тест, `Makefile`, `.gitignore`.

---

### Task 2: Объекты Kubernetes

Риск: обычный — объяснение устройства, советов с последствиями для прода мало, ошибки ловит сверка утверждений.

**Files:** Create `Docker и Kubernetes/Объекты Kubernetes.md`; Modify `.site/astro.config.mjs` — новая группа после `'Linux'` с одной страницей.

**Interfaces:** Consumes — `make k8s-*` и `reference-app:1|2`, `busybox` из задачи 1. Produces — заголовки разделов для указателя и ссылок задач 3–4.

**Обязательно на странице:**
- Control plane прогоном: `kubectl get pods -n kube-system` (apiserver, etcd, scheduler, controller-manager, kube-proxy, coredns) с подписью, что в kind они — поды на одном узле.
- Цикл согласования: Deployment на 2 реплики, `kubectl get deploy,rs,pods` и `ownerReferences` пода (`-o jsonpath`); удалённый вручную под пересоздаётся ReplicaSet. В «Частых ошибках»: удаление пода — не лечение.
- Labels и selectors: `--show-labels`, `-l`.
- Service: `get svc,endpointslices`; из пода `busybox` — запросы по короткому имени и по `<svc>.<ns>.svc.cluster.local`; несколько запросов отвечают разные поды; пояснение, что kube-proxy распределяет соединения, а не HTTP-запросы. Типы ClusterIP, NodePort, LoadBalancer (`<pending>` в kind), headless — с прогоном хотя бы ClusterIP и headless (DNS отдаёт IP подов).
- ConfigMap и Secret: в env и файлом; `kubectl get secret -o jsonpath` + `base64 -d` показывает значение открытым текстом.
- Init-контейнер и sidecar (sidecar как init-контейнер с `restartPolicy: Always`; с какой версии GA — сверить и подписать).
- Job до завершения (`COMPLETIONS 1/1`); StatefulSet — имена `<имя>-0`, `<имя>-1`; CronJob и DaemonSet — текстом.
- Ingress и Gateway API — текстом, статус ingress-nginx сверен с блогом kubernetes.io с датой.
- Namespace — по ходу демонстраций.

- [ ] Общие шаги (см. выше).

---

### Task 3: Деплой в Kubernetes

Риск: высокий — советы про пробы, лимиты и выкатку переносятся в прод, ошибка роняет сервис при деплое.

**Files:** Create `Docker и Kubernetes/Деплой в Kubernetes.md`; Modify `.site/astro.config.mjs`.

**Interfaces:** Consumes — задача 1; ссылки на разделы задачи 2. Produces — заголовки для указателя и ссылок задачи 4 и обратной ссылки из «Сигналы в Linux».

**Обязательно на странице:**
- Rolling update `reference-app:1` → `:2`: `rollout status`, `get rs` (старый ReplicaSet на 0), `maxSurge`/`maxUnavailable`.
- Без readiness: `READY_DELAY_SECONDS=20`, выкатка «успешна» сразу, а запросы через Service в это время получают `503 warming up`; с readiness-пробой — ни одного 503. Запросы — циклом из пода `busybox` с подсчётом кодов.
- Застрявшая выкатка: `:2` с `NOT_READY=1`, `maxUnavailable: 0` — старые поды продолжают отвечать, `rollout status --timeout` падает; `rollout history`, `rollout undo`. В тексте: на проде откат — это откат манифеста в репозитории.
- Пробы: liveness на `/ready` при `READY_DELAY_SECONDS` больше, чем `failureThreshold × periodSeconds` — `RESTARTS` растёт, под не становится Ready; startup-проба это чинит. Liveness на зависимость — в «Частых ошибках» с объяснением каскада.
- Requests и limits: три пода — классы QoS `BestEffort`, `Burstable`, `Guaranteed` (`.status.qosClass`). OOM и троттлинг — ссылки на задачу 4 и «Контейнеры изнутри».
- ConfigMap: смена значения не меняет env в работающем поде, `rollout restart` меняет; на проде — через изменение манифеста (аннотация с хэшем конфига).
- HPA по CPU (`CPU_BURN=1`): `get hpa` до и после масштабирования. Нестабильно — запасной путь из спеки, без выдуманного вывода.
- Остановка пода: ссылка на «Сигналы в Linux#Kubernetes при остановке пода»; прогоном — под с `preStop: sleep 10` удаляется, а в EndpointSlice его адрес виден с `ready: false`, `terminating: true`; `TERM` приходит после `preStop` (лог `got TERM`).
- Миграции при деплое: Job или init-контейнер, совместимость схемы — ссылка на «Миграции без простоя». PodDisruptionBudget — текстом.

- [ ] Общие шаги (см. выше).

---

### Task 4: Отладка в Kubernetes

Риск: высокий — ответы «что будешь делать, если» переносятся в разбор продового инцидента; вредный совет (`delete pod`, `--force`, снять лимиты) усугубляет его.

**Files:** Create `Docker и Kubernetes/Отладка в Kubernetes.md` (или две страницы при превышении 500 строк, см. Global Constraints); Modify `.site/astro.config.mjs`.

**Interfaces:** Consumes — задачи 1–3 (ссылки на разделы пробы, requests и limits, остановка пода).

**Обязательно на странице:**
- Первым разделом — общий порядок: `get` → `describe` (события внизу) → `logs`, `logs --previous` → `get events --sort-by=.lastTimestamp`. Все команды на чтение.
- Каждый сценарий вызван прогоном, вывод показывает признак, по которому его узнают:
  - `CrashLoopBackOff` (`CRASH_ON_START=1`): статус, `RESTARTS` ≥ 2 (ждать по условию), `logs --previous` с `fatal: …`, `Last State: Terminated`, `Exit Code: 1`, событие `Back-off restarting failed container`;
  - `OOMKilled` (`ALLOC_MB=200`, `limits.memory: 100Mi`): `Reason: OOMKilled`, `Exit Code: 137`; отличие от `Evicted` — текстом; ссылка на «Контейнеры изнутри#cgroups v2 и память»;
  - `Pending` (`requests.memory: 64Gi`): событие `FailedScheduling` с `Insufficient memory`;
  - `ImagePullBackOff` (`reference-app:404`): `ErrImagePull` → `ImagePullBackOff`, текст события;
  - `Running`, но не Ready (`NOT_READY=1`): `READY 0/1`, событие `Readiness probe failed … 503`, адрес в EndpointSlice с `ready: false`;
  - Service не отвечает: селектор с опечаткой — EndpointSlice без адресов; неверный `targetPort` — `connection refused` из `busybox`;
  - 502 при выкатке — по спеке, запасной путь допустим;
  - троттлинг (`CPU_BURN=1`, `limits.cpu: 200m`): `kubectl top pod` около `200m`, `nr_throttled` в `cpu.stat` растёт (`kubectl exec … cat /sys/fs/cgroup/cpu.stat` дважды).
- Как попасть внутрь: `kubectl exec`; `kubectl debug` с эфемерным контейнером `busybox` и `--target`, вывод его команды; `kubectl port-forward` в фоне с `curl` к `localhost` и остановкой в том же блоке.
- В «Частых ошибках» — `kubectl delete pod` как лечение, `--force --grace-period=0`, снятие лимитов памяти, `kubectl edit` на проде, `logs` без `--previous` у упавшего контейнера.

- [ ] Общие шаги (см. выше).

---

### Task 5: Образы Docker

Риск: высокий — советы про секреты при сборке и пользователя в образе переносятся в прод, ошибка утекает секрет в реестр.

**Files:** Create `Docker и Kubernetes/Образы Docker.md`; Modify `.site/astro.config.mjs`.

**Interfaces:** Consumes — `k8s-box.sh block` из задачи 1 (кластер не нужен). Ссылки на «Контейнеры изнутри#Слои и overlayfs», «Сигналы в Linux#Сигналы и PID 1 в контейнере».

**Обязательно на странице:**
- `docker history` собранного в блоке образа: у каких инструкций размер 0 B.
- Кэш сборки: первая сборка с `--no-cache`, вторая из кэша, затем правка кода — строки `CACHED` в `--progress=plain` (отфильтрованные) для порядка «зависимости, потом код» и обратного.
- Файл, удалённый в следующем `RUN`, остаётся в образе: размеры двух вариантов (`docker image ls --format`).
- `.dockerignore`: размер контекста сборки с ним и без.
- Многоэтапная сборка: размер до и после.
- Секрет в `ARG`/`ENV` виден в `docker history --no-trunc` или `docker image inspect`; с `RUN --mount=type=secret` — не виден.
- `USER`: `docker run --rm <образ> id` до и после.
- `ENTRYPOINT` и `CMD`: что меняют аргументы `docker run`.
- Тег и digest: `RepoDigests` у скачанного образа, перезапись локального тега другим образом.

- [ ] Общие шаги (см. выше), без `k8s-up`.

---

### Task 6: Docker на практике

Риск: обычный — локальные демонстрации, советы без необратимых действий; `prune` только с оговоркой.

**Files:** Create `Docker и Kubernetes/Docker на практике.md`; Modify `.site/astro.config.mjs`.

**Interfaces:** Consumes — `k8s-box.sh block` из задачи 1. Ссылка на «Контейнеры изнутри#cgroups v2 и память».

**Обязательно на странице:**
- Том, bind mount, tmpfs: что остаётся после пересоздания контейнера (файл в томе есть, в слое контейнера — нет).
- Сети: в сети по умолчанию имя контейнера не резолвится, в пользовательской сети `ref-net` — резолвится.
- `EXPOSE` без `-p` с хоста недоступен; `-p` и `docker port`.
- `docker logs` приложения, пишущего в stdout.
- `--memory 64m` и выделение памяти → `docker inspect -f '{{.State.OOMKilled}} {{.State.ExitCode}}'` → `true 137`.
- `--restart on-failure:3` у процесса с выходом 1 → `RestartCount 3`.
- Compose (проект `ref-compose`): `depends_on` без условия — зависимый сервис стартует до готовности соседа (сосед слушает порт после паузы); с `healthcheck` и `condition: service_healthy` — ждёт. В конце `docker compose down -v`.

- [ ] Общие шаги (см. выше), без `k8s-up`.

---

### Task 7: Указатель, оглавление и обратные ссылки

Риск: обычный — навигация, `make check` ловит битые ссылки.

**Files:**
- Create: `Docker и Kubernetes/Docker и Kubernetes.md`
- Modify: `.site/astro.config.mjs` — `'Docker и Kubernetes'` первым в группе
- Modify: `index.md` — раздел `## Docker и Kubernetes` после `## Linux` и строка в «Как читать», если там перечислены рубрики
- Modify: `Linux/Контейнеры изнутри.md`, `Linux/Сигналы в Linux.md`, `Linux/Linux.md`, `Бэкенд/Базы данных/PostgreSQL/Миграции без простоя.md`, `Бэкенд/Чек-лист бэкенда.md`

**Interfaces:** Consumes — заголовки разделов страниц задач 2–6.

**Обязательно:**
- Указатель устроен как `Linux/Linux.md`: по каждому вопросу из «Вопросов на собеседовании» страниц 2–6 — ответ на 30 секунд (2–4 предложения) со ссылкой `[[Страница#Раздел]]`; вопросы «как устроен Docker» ссылаются на «Контейнеры изнутри». В начале — версии песочницы: kind, Kubernetes, kubectl, Docker. Демонстраций и «Что спросят дальше» нет.
- `index.md` — одна строка на страницу, в стиле раздела Linux.
- Обратные ссылки по смыслу: «Контейнеры изнутри» → «Образы Docker» и «Отладка в Kubernetes» (OOM, троттлинг); «Сигналы в Linux», раздел про остановку пода — фраза «в песочнице не воспроизводится» дополняется ссылкой на прогон в «Деплой в Kubernetes»; «Linux» → указатель; «Миграции без простоя» → раздел про миграции в «Деплой в Kubernetes»; «Чек-лист бэкенда» → указатель.
- `make check`, `make build && make check-browser`.

- [ ] Написать, проверить, закоммитить одним коммитом.
