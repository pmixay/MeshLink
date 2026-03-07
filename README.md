# MeshLink

Мессенджер для локальной сети, который работает без интернета и серверов. Просто запускаешь на нескольких компьютерах в одной сети — и они находят друг друга сами.

## Что умеет

- **Автообнаружение** — устройства находят друг друга по UDP broadcast/multicast, ничего настраивать не нужно
- **Чат с шифрованием** — E2E шифрование (X25519 + AES-256-GCM), подпись сообщений (Ed25519), иконки замочка прямо в чате
- **Передача файлов** — до 2 ГБ, с докачкой при обрыве, проверка целостности через SHA-256
- **Голосовые и видеозвонки** — через WebRTC, с отображением задержки, потерь и jitter в реальном времени
- **Mesh-ретрансляция** — сообщения могут идти через промежуточные узлы (flooding с TTL и дедупликацией)
- **Сопряжение по seed-фразе** — 6-символьный код для доверенного соединения
- **Защита от спама** — rate limiting по каждому пиру + автобан + ручной blacklist
- **Хранение истории** — SQLite на устройстве, переживает перезапуски
- **Метрики и диагностика** — Security Events, Network Diagnostics, Prometheus endpoint

## Быстрый старт

```bash
pip install -r requirements.txt
python main.py
```

Откроется веб-интерфейс на `http://localhost:8080`. На втором компьютере в той же сети:

```bash
python main.py --name "Bob"
```

Через пару секунд пиры увидят друг друга.

### Тестирование на одной машине

```bash
# Терминал 1
python main.py --name "Alice" --web-port 8080 --tcp-port 5151 --media-port 5152 --file-port 5153 --discovery-port 5150

# Терминал 2
python main.py --name "Bob" --web-port 8081 --tcp-port 5161 --media-port 5162 --file-port 5163 --discovery-port 5150

# Терминал 3 (ретранслятор)
python main.py --name "Charlie" --web-port 8082 --tcp-port 5171 --media-port 5172 --file-port 5173 --discovery-port 5150
```

## Параметры запуска

| Флаг | Описание | По умолчанию |
|------|----------|--------------|
| `--name`, `-n` | Имя узла | hostname |
| `--web-port`, `-w` | Порт веб-интерфейса | 8080 |
| `--tcp-port`, `-t` | TCP порт сообщений | 5151 |
| `--media-port`, `-m` | UDP порт медиа | 5152 |
| `--file-port`, `-f` | TCP порт файлов | 5153 |
| `--discovery-port`, `-d` | UDP порт обнаружения | 5150 |
| `--no-browser` | Не открывать браузер | — |
| `--discovery-peers`, `-p` | Статический список пиров для unicast (host:port,host:port) | — |

Все параметры можно задать через переменные окружения (`MESHLINK_NODE_NAME`, `MESHLINK_WEB_PORT` и т.д.).

`MESHLINK_TRUSTED_ONLY=1` — строгий режим: чат только с seed-paired пирами.

## Архитектура

```
main.py                  — точка входа
core/
├── config.py            — конфигурация и константы
├── crypto.py            — X25519 ECDH + AES-256-GCM + Ed25519 подпись + seed-pairing
├── discovery.py         — UDP broadcast/multicast обнаружение пиров
├── messaging.py         — TCP протокол сообщений с retries и outbox
├── file_transfer.py     — передача файлов с докачкой и контролем целостности
├── media.py             — UDP аудио/видео с jitter-буфером и метриками
├── node.py              — оркестратор: связывает все подсистемы
└── storage.py           — SQLite хранилище (WAL mode)
web/
└── server.py            — Flask + Socket.IO
templates/
└── index.html           — SPA интерфейс
```

### Стек протоколов

```
┌─────────────────────────────────────┐
│        Браузер (WebRTC / UI)        │
│      Flask + Socket.IO (WS)        │
├─────────────────────────────────────┤
│         MeshNode оркестратор        │
├──────────┬────────────┬─────────────┤
│Discovery │  Messaging │ Media Engine│
│  (UDP)   │   (TCP)    │   (UDP)     │
├──────────┼────────────┼─────────────┤
│          │File Transfer│ Audio/Video│
│          │ (chunked)  │(fragmented) │
├──────────┴────────────┴─────────────┤
│    E2E: X25519 + AES-256-GCM       │
├─────────────────────────────────────┤
│         LAN (Wi-Fi / Ethernet)      │
└─────────────────────────────────────┘
```

## Подробная документация функций

### 1. Автообнаружение пиров
**Описание:** Устройства автоматически находят друг друга в локальной сети без ручной настройки.

**Реализация:**
- Используется UDP broadcast и multicast на порту 5150 (по умолчанию).
- Каждый узел периодически отправляет beacon-сообщения с информацией о себе (ID, имя, IP, порты).
- При получении beacon от нового пира, он добавляется в список известных пиров.
- Проверка доступности: пиры считаются активными, если beacon получен в последние 30 секунд (настраивается через `MESHLINK_PEER_TIMEOUT`).
- Поддержка статических пиров через `--discovery-peers` для сетей без broadcast.
- Реализовано в `core/discovery.py`: класс `PeerDiscovery` с методами `start()`, `stop()`, `_send_beacon()`, `_handle_beacon()`.

### 2. Чат с шифрованием (E2E)
**Описание:** Текстовые сообщения с end-to-end шифрованием, подписью и доставкой.

**Реализация:**
- **Шифрование:** X25519 для обмена ключами (ECDH), AES-256-GCM для симметричного шифрования сообщений.
- **Подпись:** Ed25519 для цифровой подписи каждого сообщения.
- **Протокол:** TCP на порту 5151, сообщения в формате length-prefixed JSON с полями: type, sender_id, payload, timestamp, msg_id, ttl, relay_path, signature.
- **Доставка:** Retry с экспоненциальным backoff (до 3 попыток), outbox для оффлайн-сообщений, подтверждения доставки.
- **Хранение:** SQLite база для истории чатов, с лимитами по размеру и количеству строк.
- Реализовано в `core/crypto.py` (шифрование), `core/messaging.py` (протокол), `core/storage.py` (хранение).

### 3. Передача файлов
**Описание:** Отправка файлов до 2 ГБ с докачкой и проверкой целостности.

**Реализация:**
- **Протокол:** TCP на порту 5153, chunked передача (размер chunk настраивается).
- **Докачка:** При обрыве соединения файл разбивается на части, каждая часть имеет offset и checksum (SHA-256).
- **Целостность:** Полный SHA-256 хэш файла проверяется после передачи.
- **Параллелизм:** Ограничение на количество одновременных передач (глобально и per-peer).
- **Хранение:** Файлы сохраняются в локальную директорию `downloads/`, с временными файлами в `downloads/.upload_stage/`.
- Реализовано в `core/file_transfer.py`: классы `FileSender`, `FileReceiver`, методы `send_file()`, `receive_file()`.

### 4. Голосовые и видеозвонки
**Описание:** Аудио/видео звонки через WebRTC с метриками качества.

**Реализация:**
- **WebRTC:** Используется для P2P медиа-потоков, с сигнализацией через TCP сообщения (types: WEBRTC_OFFER, WEBRTC_ANSWER, WEBRTC_ICE).
- **UDP:** Медиа на порту 5152, с jitter-буфером для компенсации задержек.
- **Метрики:** В реальном времени измеряются задержка, потери пакетов, jitter; отображаются в UI.
- **Кодеки:** Поддержка стандартных WebRTC кодеков (VP8/VP9 для видео, Opus для аудио).
- Реализовано в `core/media.py`: класс `MediaEngine` с методами `start_call()`, `handle_webrtc_offer()` и т.д.

### 5. Mesh-ретрансляция
**Описание:** Сообщения могут передаваться через промежуточные узлы для расширения сети.

**Реализация:**
- **Flooding:** Каждое сообщение имеет TTL (time-to-live, по умолчанию 8), relay_path для предотвращения циклов.
- **Дедупликация:** Сообщения с одинаковым msg_id не ретранслируются повторно.
- **Типы сообщений:** MESH_RELAY для flooding, с автоматической ретрансляцией на все известные пиры.
- **Ограничения:** Rate limiting и trust policy применяются к ретранслированным сообщениям.
- Реализовано в `core/messaging.py`: в `send_to_peer()` и обработке MESH_RELAY в `_emit()`.

### 6. Сопряжение по seed-фразе
**Описание:** Доверенное соединение с помощью короткого 6-символьного кода.

**Реализация:**
- **Генерация:** Случайный seed (6 символов, uppercase) генерируется для пира.
- **Обмен:** Seed передается out-of-band (например, голосом), затем используется для обмена публичными ключами.
- **Крипто:** Seed хэшируется с солью для создания shared secret, затем ECDH для сессионных ключей.
- **Trust:** После pairing пир помечается как trusted, позволяя чат без дополнительных проверок.
- Реализовано в `core/crypto.py`: методы `generate_pairing_seed()`, `pair_with_seed()`.

### 7. Защита от спама
**Описание:** Rate limiting, автобан и blacklist для защиты от злоупотреблений.

**Реализация:**
- **Rate limiting:** Ограничение количества сообщений per-peer (настраивается через константы в config.py).
- **Автобан:** При превышении лимитов пир автоматически банится на время.
- **Blacklist:** Ручной blacklist через API `/api/security/blacklist`.
- **Trust policy:** По умолчанию только trusted пиры (seed-paired) могут отправлять сообщения.
- Реализовано в `core/node.py`: методы `is_trusted_allowed()`, `blacklist_peer()`, и в messaging.py: семафоры для параллелизма.

### 8. Хранение истории
**Описание:** Локальная история чатов в SQLite, сохраняется между перезапусками.

**Реализация:**
- **База:** SQLite с WAL mode для производительности.
- **Лимиты:** Максимальный размер (128 MB по умолчанию) и количество строк (200k), автоматическая очистка старых записей.
- **Таблицы:** chats (сообщения), delivery_status (статус доставки), outbox (очередь отправки).
- **API:** Методы в `core/storage.py`: `persist_chat_entry()`, `load_persisted_chats()`, `enforce_db_limits()`.
- Реализовано в `core/storage.py`: класс `Storage` с методами для CRUD операций.

### 9. Метрики и диагностика
**Описание:** Сбор метрик и диагностика сети для мониторинга.

**Реализация:**
- **Метрики:** Prometheus endpoint на `/metrics`, с counters для доставки, retry, file resume и т.д.
- **Диагностика:** API `/api/network/diagnostics` возвращает статус delivery, queue, file transfer.
- **Security Events:** Лог событий безопасности (pairing, session expiry) с API для просмотра.
- **Health checks:** Endpoints `/health` и `/ready` для проверки состояния узла.
- Реализовано в `web/server.py`: маршруты для метрик и диагностики, и в core модулях: инкремент counters в storage.

## Порты

| Порт | Протокол | Назначение |
|------|----------|------------|
| 5150 | UDP | Обнаружение пиров (broadcast + multicast) |
| 5151 | TCP | Сообщения + сигнализация |
| 5152 | UDP | Медиастриминг (аудио/видео) |
| 5153 | TCP | Передача файлов |
| 8080 | HTTP/WS | Веб-интерфейс |

Если пиры не видят друг друга — проверь файрвол на этих портах. На Windows MeshLink пытается автоматически добавить правила в Windows Firewall, но может потребоваться запуск от имени администратора. Убедитесь, что обе машины находятся в одной локальной сети (один Wi-Fi или Ethernet сегмент), без VPN или разных подсетей. Если broadcast заблокирован, используйте `--discovery-peers` для статического списка IP:порт других пиров.

## Безопасность

- **Шифрование**: X25519 ECDH → AES-256-GCM с уникальным nonce на каждое сообщение
- **Подпись**: Ed25519 — каждое сообщение подписывается, получатель проверяет
- **Forward secrecy**: ротация ключей сессии (по умолчанию раз в час)
- **Seed-pairing**: 6-символьный код через PBKDF2 → доверенная сессия
- **Rate limiting**: 60 сообщений / 10 секунд на пира, автобан на 60 секунд
- **Blacklist**: ручная блокировка через UI или API
- **Целостность файлов**: SHA-256 чексума + проверка при докачке
- **Mesh relay**: подпись каждого ретранслируемого сообщения, TTL для защиты от петель, LRU дедупликация

## Mesh-ретрансляция

Каждый узел может выступать ретранслятором. Если A не видит C напрямую, но оба видят B — сообщение пройдёт через B:

```
A ──── B ──── C
       │
       └── ретранслятор
```

- Flooding с декрементом TTL (по умолчанию 5 хопов)
- Дедупликация по msg_id через LRU кеш (2000 записей)
- Адаптивный fanout при backpressure
- Подпись каждого relay-пакета

## Метрики звонка

Во время голосового или видеозвонка в интерфейсе отображаются четыре метрики, обновляемые раз в секунду через [WebRTC `getStats()` API](https://developer.mozilla.org/en-US/docs/Web/API/RTCPeerConnection/getStats).

### Задержка (Latency, мс)

**Источник данных:** приоритет отдаётся `remote-inbound-rtp.roundTripTime` (RTT по RTCP SR/RR), при отсутствии — `candidate-pair.currentRoundTripTime` (RTT по STUN ping на активной ICE-паре).

**Расчёт:**
```
RTT_raw = remote-inbound-rtp.roundTripTime × 1000  [мс]
         или candidate-pair.currentRoundTripTime × 1000  [мс]

Latency_ema[t] = 0.3 × RTT_raw[t] + 0.7 × Latency_ema[t-1]
```
Нулевые сэмплы пропускаются (STUN ещё не измерил). Экспоненциальное скользящее среднее (EMA α=0.3) сглаживает кратковременные пики без замедления реакции на устойчивый рост задержки.

### Потери пакетов (Loss, %)

**Источник данных:** `inbound-rtp` (audio) → `packetsLost`, `packetsReceived` — оба являются накопленными счётчиками с начала звонка.

**Расчёт (дельта-метод):**
```
dLost[t]  = packetsLost[t]  − packetsLost[t-1]
dRecv[t]  = packetsReceived[t] − packetsReceived[t-1]
dTotal[t] = dLost[t] + dRecv[t]

intervalLoss[t] = dLost[t] / dTotal[t] × 100  [%]  (0 если dTotal = 0)

Loss_ema[t] = 0.3 × intervalLoss[t] + 0.7 × Loss_ema[t-1]
```
Дельта-метод критически важен: наивный расчёт `packetsLost / (packetsLost + packetsReceived)` — кумулятивный и никогда не возвращается к 0 после устранения помех. Дельта-версия реагирует на **текущее** состояние линка.

### Джиттер (Jitter, мс)

**Источник данных:** `inbound-rtp.jitter` — браузер считает его самостоятельно согласно [RFC 3550 §6.4.1](https://datatracker.ietf.org/doc/html/rfc3550#section-6.4.1) как среднеквадратичное отклонение межпакетных интервалов (в секундах).

**Расчёт:**
```
jitter_raw[t] = inbound-rtp.jitter × 1000  [мс]

Jitter_ema[t] = 0.3 × jitter_raw[t] + 0.7 × Jitter_ema[t-1]
```
Браузерный `jitter` — уже бегущее среднее (exponential moving average по RFC 3550), EMA поверх него дополнительно убирает дрожание отображения.

### Битрейт (Bitrate, кбит/с)

**Источник данных:** `inbound-rtp.bytesReceived` — накопленный счётчик байт по всем входящим потокам (аудио + видео).

**Расчёт:**
```
dBytes[t]  = bytesReceived[t] − bytesReceived[t-1]
kbps_raw[t] = dBytes[t] × 8 / 1000  [кбит/с]  (интервал 1 с)

Bitrate_ema[t] = 0.3 × kbps_raw[t] + 0.7 × Bitrate_ema[t-1]
```

### Длительность звонка

Таймер запускается в момент перехода ICE-соединения в состояние `connected` (`RTCPeerConnection.onconnectionstatechange`). Инкрементируется каждую секунду через `setInterval`, отображается в формате `MM:SS`. Сбрасывается при завершении или ошибке соединения.

### Параметры EMA

| Параметр | Значение | Смысл |
|----------|----------|-------|
| α (RTC_EMA) | 0.3 | Вес нового сэмпла. Выше → быстрее реагирует, больше прыжков |
| Интервал | 1 с | Период опроса `getStats()` |
| Инициализация | 1-й сэмпл | EMA засевается первым измеренным значением (без cold-start 0) |

## Тесты

```bash
pip install -r requirements.txt
python -m pytest -q
```

Что покрыто:
- `test_messaging.py` — фрейминг, персистентность, delivery/retry
- `test_node.py` — delivery sync, trusted-only policy, security snapshot
- `test_file_transfer.py` — частичная загрузка, cleanup, retry
- `test_media.py` — метрики, статистика
- `test_e2e_load.py` — нагрузочный тест, burst send
- `test_integration_multiprocess.py` — мультипроцессный сценарий (2-3 узла)

## API

### REST

- `GET /api/info` — информация об узле
- `GET /api/peers` — список пиров
- `GET /api/chat/<peer_id>` — история чата
- `GET /api/transfers` — файловые передачи
- `POST /api/upload` — отправка файла (multipart)
- `POST /api/seed/generate` — генерация seed-фразы
- `POST /api/seed/pair` — сопряжение с пиром
- `GET /api/security/snapshot` — состояние безопасности
- `GET /api/security/events` — лог событий безопасности
- `GET /api/network/diagnostics` — диагностика сети
- `GET /metrics` — Prometheus-формат
- `GET /health` — healthcheck
- `GET /ready` — readiness probe

### Socket.IO события

Входящие: `send_message`, `typing`, `start_call`, `accept_call`, `reject_call`, `end_call`, `webrtc_offer`, `webrtc_answer`, `webrtc_ice`, `seed_pair`, `blacklist_peer`

Исходящие: `peer_joined`, `peer_left`, `message`, `message_sent`, `message_status`, `call_incoming`, `call_accepted`, `call_rejected`, `call_ended`, `media_stats`, `file_progress`, `file_complete`, `security_event`, `network_diagnostics`

## Лицензия

MIT
