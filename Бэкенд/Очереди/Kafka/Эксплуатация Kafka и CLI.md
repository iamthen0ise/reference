---
title: Эксплуатация Kafka и CLI
---

Конспект к собеседованию по повседневной работе с Kafka из консоли: создание и осмотр топиков, состояние и лаг группы, сброс offset (сначала dry-run, потом `--execute`), консольные продюсер и консьюмер с ключами и заголовками, настройка срока хранения и политики очистки, увеличение числа партиций и то, что оно ломает. Все команды запускались в контейнере `apache/kafka:4.1.0` (скрипты лежат в `/opt/kafka/bin/`), то есть выводы относятся к Kafka 4.1.0.
Рядом: [[Kafka]] · [[Консьюмер и группы Kafka]] · [[Порядок событий]] · [[Когда двигать offset]] · [[Схема событий и compaction]]
Разделы: [[#Топики]] · [[#Консольные продюсер и консьюмер]] · [[#Группы и лаг]] · [[#Сброс offset]] · [[#Настройки топика]] · [[#Увеличение числа партиций]] · [[#Бизнес-кейс]] · [[#Что спросят дальше]]

**Суть.** Всё, что приложение делает через клиентскую библиотеку, можно сделать и посмотреть консольными скриптами из дистрибутива Kafka: они нужны в инциденте, когда надо понять, где лаг, что лежит в топике и как двигать позицию группы. Обязательное правило эксплуатации: топики создаёт инфраструктура (код, который описывает инфраструктуру), а не клиент при первой отправке, потому что автосоздание даёт топик с числом партиций, сроком хранения и репликацией по умолчанию, неверными для данных. Все команды ниже получают адрес брокера флагом `--bootstrap-server localhost:9092`, он опущен в тексте команд ради краткости, а в Kafka 4.x этот флаг обязателен: подключения через ZooKeeper убраны.

## Топики

Топик создаётся с явными числом партиций, фактором репликации и настройками. Имя — по схеме `<домен>.<сущность>.<событие>`, без окружения: окружение — отдельный кластер.

```text title="Вывод"
$ kafka-topics.sh --create --topic ops.orders --partitions 3 --replication-factor 1 --config retention.ms=86400000 --config min.insync.replicas=1
WARNING: Due to limitations in metric names, topics with a period ('.') or underscore ('_') could collide. To avoid issues it is best to use either, but not both.
Created topic ops.orders.

$ kafka-topics.sh --describe --topic ops.orders
Topic: ops.orders	TopicId: _xoebzSIRU6LU3ULx3bgtA	PartitionCount: 3	ReplicationFactor: 1	Configs: min.insync.replicas=1,retention.ms=86400000
	Topic: ops.orders	Partition: 0	Leader: 1	Replicas: 1	Isr: 1	Elr: 	LastKnownElr: 
	Topic: ops.orders	Partition: 1	Leader: 1	Replicas: 1	Isr: 1	Elr: 	LastKnownElr: 
	Topic: ops.orders	Partition: 2	Leader: 1	Replicas: 1	Isr: 1	Elr: 	LastKnownElr: 
```

Предупреждение про точки и подчёркивания касается метрик: два имени, различающихся только этими символами (`a.b` и `a_b`), дают одну и ту же метрику. Поэтому в одном кластере берут один разделитель. В выводе `describe` для каждой партиции видны лидер, список реплик (`Replicas`) и подмножество, не отставшее от лидера (`Isr`). Если `Isr` короче `Replicas`, реплика отстала или брокер недоступен; запрос `kafka-topics.sh --describe --under-replicated-partitions` показывает только такие партиции (на здоровом одиночном брокере он ничего не печатает). Колонки `Elr` и `LastKnownElr` — часть появившегося в 4.x механизма «приемлемых реплик» (Eligible Leader Replicas) для лидерства после сбоя; на одиночном брокере они пусты.

**ELR на трёх брокерах.** Eligible leader replicas (приемлемые реплики лидерства, KIP-966) в Kafka 4.x запоминают, какие реплики были синхронны к моменту, когда ISR опустел: при выборе лидера контроллер берёт только их, а не отставшую реплику. Без unclean election (`unclean.leader.election.enable=false`, дефолт) после падения единственного члена ISR партиция остаётся без лидера (`Leader: none`, в `Elr` бывший лидер), а не теряет данные: лидер появляется, когда эта реплика возвращается. Состояние видно в `kafka-topics.sh --describe`, колонки `Elr` и `LastKnownElr`. Опыт со снятым выводом — [[Стенд репликации Kafka#Потеря при acks=all и min ISR 1]].

## Консольные продюсер и консьюмер

Консольный продюсер принимает строки со стандартного ввода. Ключ задаётся свойством `parse.key` с разделителем `key.separator`, заголовки — свойством `parse.headers`: заголовки идут первым полем строки, перед ключом, отделяются от неё символом `headers.delimiter` (по умолчанию табуляция), пары — `headers.separator`, ключ от значения внутри пары — `headers.key.separator`. Пустое значение записывается маркером, заданным в `null.marker`: так создаётся tombstone, см. [[Схема событий и compaction]].

```text title="Вывод"
$ printf 'trace-id:abc-1,source:billing\to-1:{"status":"created"}\n...' | kafka-console-producer.sh --topic ops.orders \
    --property parse.key=true --property key.separator=: \
    --property parse.headers=true --property headers.delimiter=$'\t' --property headers.separator=, --property headers.key.separator=:

$ kafka-console-consumer.sh --topic ops.orders --from-beginning --max-messages 3 --group ops-readers \
    --property print.key=true --property print.headers=true --property print.partition=true --property print.offset=true --property print.timestamp=true
CreateTime:1795114446649	Partition:0	Offset:0	trace-id:abc-1,source:billing	o-1	{"status":"created"}
CreateTime:1795114446653	Partition:0	Offset:1	trace-id:abc-2	o-2	{"status":"created"}
CreateTime:1795114446653	Partition:0	Offset:2	trace-id:abc-3,source:billing	o-1	{"status":"paid"}
Processed a total of 3 messages
```

Три сообщения с ключами `o-1`, `o-2`, `o-1` легли в одну партицию 0 по порядку, а заголовки (`trace-id`, `source`) видны в колонке перед ключом. Консольный консьюмер с флагом `--group` участвует в группе и коммитит offset, поэтому остаётся следом в `__consumer_offsets`; без `--group` он создаёт одноразовую группу `console-consumer-NNNNN`, и такие группы копятся в списке. Для просмотра топика без следа группы пользуются консьюмером с `--partition` и `--offset`, а для транзакционных топиков добавляют `--isolation-level read_committed`, см. [[Транзакции Kafka и exactly-once]]. Флаг `--max-messages` нужен, чтобы консьюмер вышел сам: по таймауту (`--timeout-ms`) он завершается с ошибкой `TimeoutException`, хотя читать ему уже нечего.

## Группы и лаг

Лаг группы — разница между концом журнала и зафиксированным offset по каждой партиции. Эту таблицу печатает `kafka-consumer-groups.sh --describe`. Для запущенной группы в ней видны участник, хост и клиентский идентификатор, а для остановленной — прочерки.

```text title="Вывод"
$ kafka-consumer-groups.sh --describe --group ops-readers
Consumer group 'ops-readers' has no active members.

GROUP           TOPIC           PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG             CONSUMER-ID     HOST            CLIENT-ID
ops-readers     ops.orders      1          0               1               1               -               -               -
ops-readers     ops.orders      2          0               3               3               -               -               -
ops-readers     ops.orders      0          3               3               0               -               -               -

$ kafka-consumer-groups.sh --describe --group ops-live
GROUP           TOPIC           PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG             CONSUMER-ID                                           HOST            CLIENT-ID
ops-live        ops.orders      1          1               1               0               billing-worker-1-c616d63d-2260-4479-a8ad-42ec0671d81b /192.168.65.1   billing-worker-1
ops-live        ops.orders      2          1               3               2               billing-worker-1-c616d63d-2260-4479-a8ad-42ec0671d81b /192.168.65.1   billing-worker-1
ops-live        ops.orders      0          1               3               2               billing-worker-1-c616d63d-2260-4479-a8ad-42ec0671d81b /192.168.65.1   billing-worker-1

$ kafka-consumer-groups.sh --describe --group ops-readers --state
GROUP           COORDINATOR (ID)          ASSIGNMENT-STRATEGY  STATE                #MEMBERS
ops-readers     localhost:9092  (1)       -                    Empty                0
```

Читать таблицу надо так: колонка `LAG` — сколько сообщений ждёт в партиции, а лаг, растущий только в одной партиции, указывает на «горячий» ключ или зависшую обработку именно её. Состояние группы (`STATE`): `Stable` — группа работает, `Empty` — участников нет, `PreparingRebalance` и `CompletingRebalance` — идёт ребалансировка. Подробности по участникам даёт `--describe --members --verbose` (какие партиции у кого), а `kafka-consumer-groups.sh --list` печатает все группы, включая `console-consumer-NNNNN`, оставшиеся от консольных запусков без `--group`.

## Сброс offset

Сдвинуть позицию группы можно только пока группа **неактивна** (остановлены все участники): брокер откажет активной группе, потому что иначе у участников была бы своя позиция, а у брокера другая. По умолчанию команда `--reset-offsets` ничего не меняет, а печатает план: это dry-run. Изменения применяются только с `--execute`. В Kafka 4.x при отсутствии `--execute` команда печатает предупреждение, что в будущем поведение изменится, и рекомендует писать `--dry-run` явно в скриптах.

```text title="Вывод"
$ kafka-consumer-groups.sh --group ops-readers --topic ops.orders --reset-offsets --to-earliest
WARN: No action will be performed as the --execute option is missing. In a future major release, the default behavior of this command will be to prompt the user before executing the reset rather than doing a dry run. You should add the --dry-run option explicitly if you are scripting this command and want to keep the current default behavior without prompting.

GROUP                          TOPIC                          PARTITION  NEW-OFFSET     
ops-readers                    ops.orders                     1          0              
ops-readers                    ops.orders                     2          0              
ops-readers                    ops.orders                     0          0              

$ kafka-consumer-groups.sh --group ops-readers --topic ops.orders --reset-offsets --shift-by -2 --execute
[...] WARN New offset (-2) is lower than earliest offset for topic partition ops.orders-1. Value will be set to 0 (...)
[...] WARN New offset (-2) is lower than earliest offset for topic partition ops.orders-2. Value will be set to 0 (...)

GROUP                          TOPIC                          PARTITION  NEW-OFFSET     
ops-readers                    ops.orders                     1          0              
ops-readers                    ops.orders                     2          0              
ops-readers                    ops.orders                     0          1              
```

`--shift-by -2` сдвинул позицию на два сообщения назад относительно **текущей**: в партиции 0 было 3, стало 1, а в партициях 1 и 2 текущая позиция была 0, и сдвиг ниже начала журнала обрезался до 0 с предупреждением. Из-за этой относительности `--shift-by` ненадёжен для перечитывания «последних N»; предсказуемее `--to-offset`, `--to-datetime` и `--to-earliest`. Попытка сбросить offset живой группе:

```text title="Вывод"
$ kafka-consumer-groups.sh --group ops-live --topic ops.orders --reset-offsets --to-earliest --execute

Error: Assignments can only be reset if the group 'ops-live' is inactive, but the current state is Stable.

GROUP                          TOPIC                          PARTITION  NEW-OFFSET     
```

Порядок действий в инциденте: остановить группу (масштабировать до нуля), выполнить `--reset-offsets` без `--execute` и прочитать план, затем с `--execute`, проверить `--describe`, запустить группу. Сброс `--to-earliest` на группе, обработчик которой не идемпотентен, повторит все эффекты: поэтому сначала убеждаются в идемпотентности (см. [[Когда двигать offset]]).

## Настройки топика

Срок хранения, политика очистки и прочие настройки топика меняются `kafka-configs.sh` на лету, без пересоздания. Значения со списком (`cleanup.policy=compact,delete`) в команде берут в квадратные скобки, иначе запятая принимается за разделитель настроек и команда отвергается.

```text title="Вывод"
$ kafka-configs.sh --entity-type topics --entity-name ops.orders --alter --add-config retention.ms=3600000,cleanup.policy=compact,delete
requirement failed: Invalid entity config: all configs to be added must be in the format "key=val" or  "key=[val1,val2]" to group values which contain commas.

$ kafka-configs.sh --entity-type topics --entity-name ops.orders --alter --add-config 'retention.ms=3600000,cleanup.policy=[compact,delete]'
Completed updating config for topic ops.orders.

$ kafka-configs.sh --entity-type topics --entity-name ops.orders --describe
Dynamic configs for topic ops.orders are:
  cleanup.policy=compact,delete sensitive=false synonyms={DYNAMIC_TOPIC_CONFIG:cleanup.policy=compact,delete, DEFAULT_CONFIG:log.cleanup.policy=delete}
  min.insync.replicas=1 sensitive=false synonyms={...}
  retention.ms=3600000 sensitive=false synonyms={DYNAMIC_TOPIC_CONFIG:retention.ms=3600000}

$ kafka-configs.sh --entity-type topics --entity-name ops.orders --alter --delete-config retention.ms
$ kafka-configs.sh --entity-type topics --entity-name ops.orders --describe --all | grep -E 'retention.ms|cleanup.policy|segment.ms'
  cleanup.policy=compact,delete sensitive=false synonyms={...}
  retention.ms=604800000 sensitive=false synonyms={}
  segment.ms=604800000 sensitive=false synonyms={}
```

В `synonyms` видна цепочка источников значения: динамическая настройка топика перекрывает значение по умолчанию брокера. После `--delete-config` значение вернулось к умолчанию в 604 800 000 мс (7 суток), а не к тому, с чем топик был создан (86 400 000 мс): создание с `--config` тоже пишет динамическую настройку, и она удаляется вместе с остальными. Политика `compact,delete` объединяет сжатие по ключу и удаление по сроку, подробнее в [[Схема событий и compaction]]. Уменьшение `retention.ms` удаляет старые сегменты не мгновенно, а при ближайшей проверке (раз в `log.retention.check.interval.ms`, по умолчанию 5 минут), и активный сегмент не удаляется, пока не закроется.

## Увеличение числа партиций

Число партиций можно только увеличивать. Уменьшить его нельзя: команда отвечает ошибкой, а единственный путь вниз — новый топик и перенос данных. А вот увеличение, на первый взгляд безобидное, ломает порядок по ключу. Эксперимент: топик из 3 партиций, шесть ключей, по одному сообщению `v1` на ключ; затем топик расширили до 6 партиций и отправили `v2` на те же ключи.

```text title="Вывод"
$ kafka-topics.sh --alter --topic ops.keys --partitions 6
$ kafka-topics.sh --describe --topic ops.keys | head -1
Topic: ops.keys	TopicId: 9a1R8ni4SLaywxVMTjLmkg	PartitionCount: 6	ReplicationFactor: 1	Configs: min.insync.replicas=1

$ kafka-topics.sh --alter --topic ops.keys --partitions 3
Error while executing topic command : The topic ops.keys currently has 6 partition(s); 3 would not be an increase.
[...] ERROR org.apache.kafka.common.errors.InvalidPartitionsException: The topic ops.keys currently has 6 partition(s); 3 would not be an increase.
```

```text title="Вывод"
$ kafka-console-consumer.sh --topic ops.keys --from-beginning --max-messages 12 --property print.key=true --property print.partition=true --property print.offset=true | sort -k3,3 -s
Partition:2	Offset:0	c-1	v1
Partition:5	Offset:0	c-1	v2
Partition:1	Offset:0	c-2	v1
Partition:4	Offset:0	c-2	v2
Partition:1	Offset:1	c-3	v1
Partition:1	Offset:4	c-3	v2
Partition:1	Offset:2	c-4	v1
Partition:4	Offset:1	c-4	v2
Partition:1	Offset:3	c-5	v1
Partition:1	Offset:5	c-5	v2
Partition:0	Offset:0	c-6	v1
Partition:3	Offset:0	c-6	v2
```

Ключи `c-3` и `c-5` остались в партиции 1, а `c-1`, `c-2`, `c-4` и `c-6` переехали: `v1` лежит в одной партиции, `v2` — в другой. Теперь у ключа `c-1` два журнала, и консьюмеры, читающие партиции 2 и 5, никак не согласуют порядок: `v2` может быть обработано раньше `v1`, а в сжатом топике (compaction) на ключе окажутся две «последние» записи в разных партициях. Так хеш `murmur2(key) % partitions` меняет результат при смене знаменателя. Что делать. Закладывать число партиций с запасом при создании (рост потока на 2–3 года, но не «на всякий случай» тысячами: каждая партиция увеличивает время ребалансировки и нагрузку на метаданные). Если расширять всё же нужно, делают это окном: останавливают консьюмеров после обработки всего накопленного, расширяют топик, запускают. Для критичного порядка заводят новый топик с нужным числом партиций, дублируют в него поток и переключают потребителей, когда старый вычитан до конца. Консьюмеры с состоянием по ключу (кэш, агрегаты) при расширении теряют это состояние, а новые партиции получают «чужие» ключи.

## Бизнес-кейс

Ночью вырос лаг на воркере начислений. Дежурный смотрит `--describe` по группе: лаг один и тот же на всех партициях и растёт, значит, воркер не справляется или упал, а не «горячий» ключ. Через `--state` видно `Stable` и участников нет — группа пуста, под не поднялся. После исправления остаётся принять решение: лаг в несколько часов читать подряд или сбросить позицию на «сейчас» (`--to-latest`), пропустив события? Для денег пропускать нельзя, значит читать и следить, чтобы база выдержала разгон (см. [[Повторные попытки при падении базы]]). Отдельный вопрос эксплуатации, который решает продукт: сколько хранить сообщения. Срок хранения должен быть больше самого долгого допустимого простоя консьюмеров, иначе после простоя часть сообщений будет удалена до прочтения.

## Что спросят дальше

- Как перечитать топик заново? Новая группа с `auto_offset_reset=earliest` либо `--reset-offsets --to-earliest --execute` на остановленной группе.
- Почему `--reset-offsets` отказывает? Группа активна: остановите всех участников; offset нельзя менять под работающими консьюмерами.
- Можно ли уменьшить число партиций? Нет, только новый топик и перенос. Увеличить можно, но порядок по ключу на момент перехода нарушается.
- Как посмотреть, что лежит в топике, не создавая следа группы? Консольный консьюмер с `--partition` и `--offset` без `--group`, либо с `--group`, но осознанно.
- Зачем топики создавать заранее? Автосоздание даёт топик с числом партиций и сроком хранения по умолчанию; неверное число партиций потом не исправить без последствий для порядка.
