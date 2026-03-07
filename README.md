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
python main.py --name "Alice" --web-port 8080 --tcp-port 5151 --media-port 5152 --file-port 5153

# Терминал 2
python main.py --name "Bob" --web-port 8081 --tcp-port 5161 --media-port 5162 --file-port 5163

# Терминал 3 (ретранслятор)
python main.py --name "Charlie" --web-port 8082 --tcp-port 5171 --media-port 5172 --file-port 5173
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
| `--verbose`, `-v` | Подробные логи | — |

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

## Порты

| Порт | Протокол | Назначение |
|------|----------|------------|
| 5150 | UDP | Обнаружение пиров (broadcast + multicast) |
| 5151 | TCP | Сообщения + сигнализация |
| 5152 | UDP | Медиастриминг (аудио/видео) |
| 5153 | TCP | Передача файлов |
| 8080 | HTTP/WS | Веб-интерфейс |

Если пиры не видят друг друга — проверь файрвол на этих портах.

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
