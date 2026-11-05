# Защита от превышения лимитов Telegram API

## Обзор изменений

Добавлена комплексная защита от превышения лимитов Telegram API для предотвращения блокировки аккаунта во **всех точках входа**:

1. **`telegram_client.py`** - Python клиент для скриптов (HTTP API через Python)
2. **`telegram_core.py`** - HTTP API сервер (api.py)
3. **`main.py`** - MCP сервер (Model Context Protocol)

Все три компонента используют одинаковые механизмы защиты, что гарантирует невозможность обхода лимитов.

## Реализованные механизмы защиты

### 1. Обработка FloodWaitError
- **Автоматическое ожидание:** При получении FLOOD_WAIT ошибки клиент автоматически ждет указанное время
- **Повторные попытки:** До 3 автоматических повторных попыток после ожидания
- **Извлечение времени:** Из параметров ответа или текста ошибки (формат: FLOOD_WAIT_123)
- **Максимальное ожидание:** Ограничено 3600 секундами (1 час)

### 2. Автоматические задержки между запросами
- **Минимальная задержка:** 0.2 секунды между запросами (5 req/s) по умолчанию
- **Jitter:** Добавлен случайный jitter (0-0.05 сек) для избежания синхронизации
- **Настраиваемо:** Можно изменить через параметр `min_request_delay` при создании клиента

### 3. Обработка ошибок 429 (Rate Limit)
- **Автоматический retry:** До 3 попыток с exponential backoff
- **Извлечение retry_after:** Из ответа API или заголовков
- **Jitter:** Добавлен случайный jitter (0-25%) для избежания thundering herd
- **Максимальное ожидание:** Ограничено 60 секундами

### 4. Лимит сообщений в чат
- **1 сообщение в секунду:** Автоматическая проверка и ожидание перед отправкой
- **Отслеживание по чатам:** Отдельный счетчик для каждого чата
- **Применяется к:** 
  - `send_message()` - отправка текстовых сообщений
  - `send_file()` - отправка файлов
  - `send_voice()` - отправка голосовых сообщений
  - `send_sticker()` - отправка стикеров
  - `send_gif()` - отправка GIF
  - `forward_message()` - пересылка сообщений
  - `reply_to_message()` - ответ на сообщение
  - `create_poll()` - создание опросов (отправка сообщения с опросом)
  - `send_reaction()` - отправка реакций (общий rate limit)
  - `remove_reaction()` - удаление реакций (общий rate limit)

### 5. Лимит редактирования
- **5 редактирований в секунду:** Автоматическая проверка
- **120 редактирований в час:** Отслеживание и блокировка при превышении
- **Применяется к:** `edit_message()`

### 6. Защита операций чтения
- **Глобальный rate limit:** Минимум 0.2 сек между любыми запросами (включая чтение)
- **Обработка FloodWaitError:** Автоматическое ожидание и повтор при FLOOD_WAIT ошибках
- **Exponential backoff:** При ошибках чтения делается до 3 попыток с увеличивающейся задержкой
- **Реальные повторные попытки:** `_mcp_protected_read_operation()` принимает callable (lambda), что позволяет создавать новый coroutine на каждую попытку для корректной работы retry
- **Применяется к:**
  - `get_messages()` - получение сообщений с пагинацией
  - `get_history()` - получение истории чата
  - `search_messages()` - поиск сообщений
  - `list_messages()` - список сообщений с фильтрами
  - `get_message_context()` - контекст вокруг сообщения
  - `get_pinned_messages()` - закрепленные сообщения
  - `get_media_info()` - информация о медиа
  - `download_media()` - скачивание медиа
  - `list_topics()` - список топиков форума
  - `get_user_photos()` - фото пользователя
  - `get_user_status()` - статус пользователя
  - `get_recent_actions()` - последние действия админов
  - `search_public_chats()` - поиск публичных чатов
  - `resolve_username()` - разрешение username
  - `export_contacts()` - экспорт контактов
  - `get_blocked_users()` - список заблокированных
  - `get_entity()` - получение информации о сущности (используется везде)
  - `get_dialogs()` - список диалогов
  - И другие операции чтения

### 7. Exponential Backoff
- При ошибках запрос повторяется с увеличивающейся задержкой
- Формула: `wait_time = retry_after * (2 ^ attempt) + jitter`
- Максимум 3 попытки
- Применяется как к операциям записи, так и к операциям чтения

## Архитектура защиты

### Точки входа и их защита

1. **HTTP API через Python клиент** (`telegram_client.py`)
   - Используется скриптами в your own scripts
   - Защита: автоматические задержки, проверка лимитов, обработка ошибок

2. **HTTP API сервер** (`telegram_core.py`)
   - Используется через `api.py` (FastAPI)
   - Защита: те же механизмы, но асинхронные (async/await)

3. **MCP сервер** (`main.py`)
   - Используется через Model Context Protocol (Claude Desktop, Cursor)
   - Защита: те же механизмы, но асинхронные (async/await)

**Важно:** Все три компонента используют **независимые счетчики лимитов**, что означает, что лимиты применяются на уровне каждого компонента отдельно. Это обеспечивает дополнительную защиту.

## Использование

### Базовое использование (защита включена по умолчанию)

#### Python клиент (для скриптов)

```python
from telegram_client import TelegramClient

client = TelegramClient()
client.send_message(chat_id=123456, message="Hello!")
```

#### HTTP API (через curl или другой HTTP клиент)

```bash
# Защита применяется автоматически на сервере
curl -X POST http://localhost:8080/messages/send \
  -H "Content-Type: application/json" \
  -d '{"chat_id": 123456, "message": "Hello!"}'
```

#### MCP сервер (через Claude/Cursor)

Защита применяется автоматически при использовании инструментов MCP.

### Настройка параметров

```python
from telegram_client import TelegramClient

# Увеличить задержку между запросами (более безопасно)
client = TelegramClient(
    min_request_delay=0.3,  # 3.3 req/s вместо 5 req/s
    max_retries=5  # Больше попыток при ошибках
)
```

### Обработка ошибок

```python
from telegram_client import TelegramClient, RateLimitError, FloodWaitError, TelegramClientError

client = TelegramClient()

try:
    client.send_message(chat_id=123456, message="Hello!")
except RateLimitError as e:
    print(f"Rate limit! Wait {e.retry_after:.1f} seconds")
    time.sleep(e.retry_after)
    # Повторить запрос
except FloodWaitError as e:
    print(f"Flood wait! Wait {e.wait_time:.1f} seconds")
    time.sleep(e.wait_time)
    # Повторить запрос
except TelegramClientError as e:
    print(f"API error: {e}")
```

**Примечание:** `FloodWaitError` имеет атрибут `wait_time` и `retry_after` (для совместимости).

## Лимиты Telegram API (2026)

### Отправка сообщений
- **В любом чате:** ~1 сообщение в секунду
- **В групповых чатах:** до 20 сообщений в минуту
- **При рассылке:** до 30 сообщений в секунду (broadcasting)

### Редактирование
- **В секунду:** до 5 редактирований
- **В час:** до 120 редактирований

### API запросы
- **Общий лимит:** ~30 запросов в секунду
- **При превышении:** HTTP 429 с `retry_after`

## Рекомендации

1. **Не уменьшайте `min_request_delay`** ниже 0.2 секунд
2. **Обрабатывайте `RateLimitError`** в вашем коде
3. **Избегайте массовых операций** без пауз
4. **Используйте задержки** при работе с несколькими чатами
5. **Мониторьте ошибки** и логируйте случаи rate limiting

## Обновление скриптов

Все скрипты в your own scripts должны:
1. Импортировать `RateLimitError` и `FloodWaitError`: `from telegram_client import ..., RateLimitError, FloodWaitError`
2. Обрабатывать обе ошибки (можно вместе, так как у них одинаковый интерфейс)

Пример:
```python
except (RateLimitError, FloodWaitError) as e:
    wait_time = getattr(e, 'retry_after', getattr(e, 'wait_time', 1.0))
    error_type = "Rate limit" if isinstance(e, RateLimitError) else "Flood wait"
    print(json.dumps({
        "error": f"{error_type} exceeded: {e}",
        "retry_after": wait_time,
        "wait_time": wait_time,
        "message": f"Подождите {wait_time:.1f} секунд"
    }, ensure_ascii=False))
    sys.exit(1)
```

## Тестирование

Для проверки работы защиты:
1. Запустите скрипт, который делает много запросов
2. Проверьте, что задержки работают
3. При ошибке 429 проверьте, что retry работает корректно

## Примечания

- **Защита работает автоматически** для всех методов во всех компонентах
- **Задержки применяются прозрачно**, без изменения API
- **Jitter помогает избежать синхронизации** при множественных клиентах
- **Отслеживание лимитов работает в памяти** (сбрасывается при перезапуске)
- **Независимые счетчики** в каждом компоненте обеспечивают дополнительную защиту
- **Все три точки входа защищены**, невозможно обойти лимиты через другой компонент

## Гарантии защиты

✅ **Невозможно обойти лимиты** через:
- Прямые вызовы HTTP API (защищено в `telegram_core.py`)
- Python клиент (защищено в `telegram_client.py`)
- MCP сервер (защищено в `main.py`)
- Параллельные запросы (jitter предотвращает синхронизацию)

✅ **Автоматическая обработка**:
- FLOOD_WAIT ошибок (Telethon)
- HTTP 429 ошибок (HTTP API)
- Повторные попытки с exponential backoff

✅ **Соблюдение всех лимитов**:
- 1 сообщение/сек в чат (для всех типов отправки: текст, файлы, голос, стикеры, GIF, опросы, пересылка, ответы)
- 5 редактирований/сек, 120/час
- ~5 запросов/сек глобально (настраиваемо) - применяется ко ВСЕМ операциям (чтение и запись)
- Реакции защищены общим rate limiting
- Все операции чтения защищены глобальным rate limit и обработкой FloodWaitError

✅ **Защищенные методы в MCP сервере** (`main.py`):
- **Операции записи:**
  - `send_message`, `send_file`, `send_voice`, `send_sticker`, `send_gif`
  - `forward_message`, `reply_to_message`, `create_poll`
  - `edit_message`
  - `send_reaction`, `remove_reaction` (общий rate limit)
- **Операции чтения:**
  - `get_messages`, `get_history`, `search_messages`, `list_messages`
  - `get_message_context`, `get_pinned_messages`, `get_media_info`
  - `download_media`, `list_topics`, `get_user_photos`, `get_user_status`
  - `get_recent_actions`, `search_public_chats`, `resolve_username`
  - `export_contacts`, `get_blocked_users`, `get_dialogs`
  - `get_entity` (используется во всех методах)

✅ **Защищенные методы в HTTP API** (`telegram_core.py`):
- **Операции записи:** `send_message`, `forward_message`, `edit_message`
- **Операции чтения:** `get_messages` (с обработкой FloodWaitError)

✅ **Защищенные методы в Python клиенте** (`telegram_client.py`):
- Все методы отправки и редактирования через единый `_request()` метод
