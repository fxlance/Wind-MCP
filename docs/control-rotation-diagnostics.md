# Ротация hooks и CONTROL_PREEMPTED

В `0.8.8+fxlance.1` (`35839b7dd0fc0288995c04b26d2cd4c602c2c3b1`)
монитор каждые 5 секунд заменяет оба hooks, устанавливая `_rotating=True`.
`checkpoint()` немедленно отклонял шаг, попавший в этот короткий интервал:
`CONTROL_PREEMPTED: ai`, даже при прежнем поколении и работающей защите.
`ai` в тексте — состояние координатора; оно не определяет виновника.

`checkpoint()` отклоняет шаг при любом из условий: state != ai, token !=
generation, fast_takeover, fast_pending, rotating, emergency, suppression off.
Если одновременно сохранён Escape latch, код — `USER_CANCELLED`.
Второй путь `CONTROL_PREEMPTED` — преобразование `InputUnavailable` в middleware,
когда состояние не unavailable и отмены Escape нет.

В `.2` только изолированная ротация допускает ожидание её события до 0,5 секунды.
Ожидание проходит вне coordinator lock, каждый раз проверяются все прежние условия.
Поколение не меняется, новый ввод до завершения ожидания не выполняется. Escape и
fault будят ожидающий шаг. Таймаут и вызов с самого monitor thread отклоняются;
неудачная установка публикует fail-open до пробуждения ожидающего. begin_call
сохраняет прежний отказ CONTROL_UNAVAILABLE при ротации до создания lease.

## Приватная диагностика перед новым запуском

Координатор хранит 256 событий: state transitions, lease, visual acknowledgement,
capture pause, rotation begin/end, физический Escape/takeover, fail-open с причиной,
watchdog faults, первые и последующие отказы. Каждое содержит sequence ID,
monotonic_ns, thread ID, token (может быть null на monitor thread), generation,
rotation ID и flags. Первые fault/failure сохраняются отдельно от вторичных ошибок.
`ControlBlocked.status.native_diagnostic` содержит requested_token и точный список
failed_predicates из проверки. Middleware добавляет причину input_ledger_blocked.

Все hook callbacks только добавляют записи в ограниченную память; они не пишут
файлы и не получают диагностический lock. `diagnostic()` — advisory snapshot,
не основание разрешать ввод. Журнал не содержит клавиш/текста, координат,
названий окон, URL или browser данных. Physical Escape означает non-injected
Win32 key-down 0x1B, а не установление личности человека за клавиатурой.

Экспорт нужно включить **до native lifespan** и закрыть **после него**:

```python
from windows_mcp.desktop.control_trace import ControlTraceWriter

with ControlTraceWriter(controller, private_root / f"native-{procedure_id}.ndjson",
                        run_id=run_id, procedure_id=procedure_id):
    async with mcp._lifespan_manager():
        result = await mcp.call_tool("ChromeUiaScenario", {})
```

В начале синхронно записывается header с UUID и hashes фактически импортированного
native кода. Отдельный worker выгружает журнал примерно каждые 20 ms, flush после
каждой выгрузки. Он не получает coordinator lock, чтобы сам журнал не провоцировал
watchdog fail-open. Существующий файл не перезаписывается. Ошибка/зависание sink
останавливает native control. При резком hard termination последние события после
последнего flush могут отсутствовать; это ограничение явно учитывать.

После первого отказа сохранить полный status и отдельный first-native-failure.json
до GUI cleanup; последующие ошибки записать отдельно. Сохранить NDJSON до архивирования
пакета. Внешний сценарий сохраняет run/procedure IDs и первичный статус USER_CANCELLED
при Escape. Ни retry, ни resume, ни очистка Escape latch этим исправлением не разрешаются.

Для подключения в закреплённом downstream нужны новый точный commit/version,
пересборка offline runtime, сверка manifest/hashes и отдельная приёмка. Эти исходники
не меняют уже установленный runtime, prepared пакет или текущие разрешения запуска.

## Проверки

`python tests/offline_control_fixture.py --baseline` с WIND_CONTROL_SOURCE,
указывающим на `.1`, воспроизводит гонку прямо в её periodic monitor branch.
Все DLL и функции ввода заменены заглушками; неизвестный native вызов вызывает
AssertionError. Fixture запускается напрямую, без pytest conftest.

`python tests/offline_control_fixture.py -v`: 15 проверок handshake/timeout,
Escape latch, поколения, suppression, faults, primary/secondary evidence и sink.
`python tests/offline_control_fixture.py --existing` в среде с pytest:
34 существующие проверки control/rotation в том же полностью тихом окружении.
Pytest plugins выключены; asyncio_mode warning ожидаем, async tests здесь нет.
Native desktop/hooks/input/browser не запускаются. Живая приёмка `.2` не выполнена.
