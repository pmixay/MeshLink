# Hex.Team MeshLink

Децентрализованная система связи для локальной сети (LAN) и прямых P2P-соединений: обнаружение узлов, защищённый чат, передача файлов, real-time коммуникация и диагностика без центрального сервера.

Документ подготовлен в формате, удобном для защиты проекта по ТЗ «Hex.Team — Система децентрализованной связи».

---

## 1) Цель проекта

`MeshLink` решает задачу связи в условиях, когда централизованная инфраструктура недоступна или нежелательна:

- локальные мероприятия с перегрузкой сети;
- закрытые площадки/сегменты;
- аварийные и офлайн-сценарии.

Система строит P2P/mesh-коммуникацию между узлами в одной сети, обеспечивая обмен сообщениями, файлами и коммуникацию в реальном времени с контролем надёжности и безопасности.

---

## 2) Что реализовано

- **Обнаружение узлов** (UDP broadcast/multicast + статические пиры).
- **Установка P2P-сессии** между узлами.
- **Текстовый обмен** в обе стороны.
- **Мультихоп/ретрансляция** сообщений (flooding + TTL + дедупликация).
- **Передача файлов** (чанки, SHA-256, докачка, статусы).
- **Real-time связь** (голос/видео через WebRTC) + live-метрики.
- **Надёжность** (ACK, очереди, retry, outbox, backpressure).
- **Безопасность** (шифрование, подпись, pairing, anti-spam, blacklist).
- **Диагностика** (`/metrics`, network/security endpoints, health probes).

---

## 3) Технологический стек

- **Backend:** Python 3.10+
- **Web/API:** Flask + Socket.IO
- **P2P транспорт:**
  - UDP — discovery + media transport;
  - TCP — messaging + file transfer.
- **Криптография:** X25519 (ECDH), AES-256-GCM, Ed25519.
- **Хранение:** SQLite (WAL).
- **UI:** SPA в `templates/index.html`.

---

## 4) Архитектура

```text
main.py                  — точка входа
core/
├── config.py            — конфигурация и константы
├── crypto.py            — X25519 + AES-256-GCM + Ed25519 + seed-pairing
├── discovery.py         — обнаружение узлов (UDP)
├── messaging.py         — TCP messaging, ACK/retry/outbox/relay
├── file_transfer.py     — чанковая передача файлов, resume, integrity
├── media.py             — real-time медиа и метрики качества
├── node.py              — оркестратор подсистем
└── storage.py           — SQLite storage
web/
└── server.py            — HTTP API + Socket.IO
templates/
└── index.html           — клиентский интерфейс
```

### Топология

Поддерживаемая топология — **mesh в пределах LAN**.

Пример:

```text
A ─── B ─── C
     / \
    D   E
```

- узлы обнаруживаются динамически;
- при исчезновении узла peer timeout удаляет/помечает его как offline;
- при недоступности прямого пути используется ретрансляция (при наличии связного графа).

---

## 5) Запуск

### Быстрый старт

```bash
pip install -r requirements.txt
python main.py
```

UI: `http://localhost:8080`

### Демо на одной машине (3 узла)

```bash
# Узел A
python main.py --name "Alice" --web-port 8080 --tcp-port 5151 --media-port 5152 --file-port 5153 --discovery-port 5150

# Узел B
python main.py --name "Bob" --web-port 8081 --tcp-port 5161 --media-port 5162 --file-port 5163 --discovery-port 5150

# Узел C (ретранслятор)
python main.py --name "Charlie" --web-port 8082 --tcp-port 5171 --media-port 5172 --file-port 5173 --discovery-port 5150
```

### Параметры CLI

| Флаг | Назначение | По умолчанию |
|---|---|---|
| `--name`, `-n` | Имя узла | hostname |
| `--web-port`, `-w` | Порт web UI/API | 8080 |
| `--tcp-port`, `-t` | TCP messaging/signaling | 5151 |
| `--media-port`, `-m` | UDP media | 5152 |
| `--file-port`, `-f` | TCP file transfer | 5153 |
| `--discovery-port`, `-d` | UDP discovery | 5150 |
| `--discovery-peers`, `-p` | Статический список host:port | — |
| `--no-browser` | Не открывать браузер автоматически | — |

Ключевые env-переменные: `MESHLINK_NODE_NAME`, `MESHLINK_WEB_PORT`, `MESHLINK_TRUSTED_ONLY` и др.

---

## 6) Соответствие обязательным сценариям ТЗ

| Сценарий ТЗ | Статус | Где реализовано |
|---|---|---|
| 1. Обнаружение узлов и список устройств | ✅ | `core/discovery.py`, `GET /api/peers` |
| 2. Установка P2P-сессии | ✅ | `core/node.py`, `core/messaging.py` |
| 3. Обмен текстовыми сообщениями в обе стороны | ✅ | `core/messaging.py`, Socket.IO `send_message` |
| 4. Мультихоп-цепочка / ретранслятор | ✅ | relay в `core/messaging.py` (TTL + dedup) |
| 5. Передача файла с контролем целостности и статусом | ✅ | `core/file_transfer.py`, `file_progress`, `file_complete` |
| 6. Real-time коммуникация (голос/видео) | ✅ | WebRTC signaling + метрики в `templates/index.html` |

---

## 7) Протокол и надёжность

### Сообщения

- Идентификаторы сообщений (`msg_id`) для дедупликации.
- TTL + relay path для защиты от петель.
- ACK/статусы доставки.
- Retry с ограничениями и backoff.
- Outbox/очередь для временно недоступных пиров.

### Реакция на сбои

- peer timeout и обновление списка узлов;
- ретраи при недоставке;
- сохранение истории и служебного состояния в SQLite;
- диагностика queue/delivery/file transfer через API.

---

## 8) Real-time звонки и метрики

### Транспорт и буферизация

- signaling: Socket.IO + message channel;
- медиа: WebRTC (браузерный стек), UDP-транспорт;
- jitter/latency/loss/bitrate в интерфейсе.

### Методика замеров

Вызов `RTCPeerConnection.getStats()` раз в 1 секунду.

Используется EMA-сглаживание (α = 0.3) для:

- RTT/latency;
- loss (дельта-подход по счётчикам);
- jitter;
- bitrate.

Пользовательский UI отображает текущие значения и остаётся интерактивным при потере пакетов (изменения качества видны в метриках).

---

## 9) Передача файлов

Поддерживаемые механики:

- чанковая передача;
- контроль целостности (SHA-256);
- подтверждения/повторы;
- докачка после разрыва;
- частично завершённые передачи;
- ограничения нагрузки (размер чанка, параллелизм, контроль активных отправок).

Диагностика доступна через `/api/network/diagnostics` и `/api/transfers`.

---

## 10) Безопасность

### Реализовано

- шифрование трафика: X25519 + AES-256-GCM;
- подпись сообщений: Ed25519;
- базовая идентификация узлов: node_id + pairing;
- trust onboarding: seed pairing;
- защита от злоупотреблений: rate limit, autoban, blacklist;
- меры против подмены/петель в relay (подпись + TTL + dedup).

### Короткая модель угроз

Система учитывает:

- подмену/модификацию сообщений;
- повторную доставку старых пакетов;
- спам/флуд со стороны узлов;
- частичную деградацию сети (loss, jitter, разрывы).

---

## 11) Документация и тестируемость

В репозитории присутствуют:

- исходный код;
- этот README;
- архитектурное описание в структуре модулей;
- endpoints для логов/метрик/диагностики;
- набор тестов:
  - `tests/test_messaging.py`
  - `tests/test_node.py`
  - `tests/test_file_transfer.py`
  - `tests/test_media.py`
  - `tests/test_e2e_load.py`
  - `tests/test_integration_multiprocess.py`

Запуск тестов:

```bash
python -m pytest -q
```

---

## 12) API (для демонстрации)

### REST

- `GET /api/info`
- `GET /api/peers`
- `GET /api/chat/<peer_id>`
- `GET /api/transfers`
- `POST /api/upload`
- `POST /api/seed/generate`
- `POST /api/seed/pair`
- `GET /api/security/snapshot`
- `GET /api/security/events`
- `GET /api/network/diagnostics`
- `GET /metrics`
- `GET /health`
- `GET /ready`

### Socket.IO

Входящие: `send_message`, `typing`, `start_call`, `accept_call`, `reject_call`, `end_call`, `webrtc_offer`, `webrtc_answer`, `webrtc_ice`, `seed_pair`, `blacklist_peer`.

Исходящие: `peer_joined`, `peer_left`, `message`, `message_sent`, `message_status`, `call_incoming`, `call_accepted`, `call_rejected`, `call_ended`, `media_stats`, `file_progress`, `file_complete`, `security_event`, `network_diagnostics`.

---

## 13) План демонстрации на защите

1. Запуск 3 узлов и показ auto-discovery.
2. P2P чат A↔B и подтверждение доставки.
3. Отключение прямой связности A↔C и демонстрация мультихоп через B.
4. Передача файла с отображением прогресса и проверкой SHA-256.
5. Голосовой вызов, демонстрация latency/loss/jitter/bitrate.
6. Провокация потерь (например, ограничение сети), показ устойчивости и метрик.
7. Показ security events, diagnostics и `/metrics`.

---

## 14) Ограничения и честные рамки

- Основной режим: **LAN / единый сетевой контур**.
- NAT traversal / BLE / Wi‑Fi Direct fallback в базовой версии не являются целевым сценарием.
- Качество real-time зависит от свойств сети и браузерного WebRTC стека.

---

## 15) Лицензия

`The Unlicense`
