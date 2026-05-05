# SMC Crypto Scanner — Signal Engine

Автоматический сканер криптовалютных сигналов на основе Smart Money Concepts и Institutional Price Action (CMS / Mansor Sapari).

**Не торговый бот.** Генерирует, хранит, оценивает сигналы и отправляет Telegram-алерты с графиками.

---

## Что умеет

- Полный SMC-пайплайн: sweep → BOS → OB/FVG → 7-слойное подтверждение
- Институциональные паттерны: Quasimodo, Fakeout V1/V2/V3, SR Flip, Compression, MPL
- HTF alignment, Kill Zones, Premium/Discount зоны
- Мультимодельный walk-forward бэктестер с HTML-отчётом
- Виртуальный портфель с трейлинг-стопом и compound PnL
- Ночной отчёт в Telegram (день / неделя / месяц / all-time)
- Автозапуск (Windows Task Scheduler / Linux systemd)

---

## Структура проекта

```
smc_scanner/
│
├── main.py             — точка входа, CLI, планировщик
├── config.py           — все настройки (единый источник правды)
│
├── Core pipeline
│   ├── datafeed.py     — CCXT OHLCV (Binance по умолчанию)
│   ├── structure.py    — свинги HH/HL/LH/LL, тренд, BOS/MBOS, HTF confluence
│   ├── liquidity.py    — ликвидностные зоны, equal H/L, sweeps
│   ├── orderblocks.py  — Order Block и FVG, отслеживание митигации
│   └── signals.py      — сборка сигнала (entry / SL / TP)
│
├── Фильтрация
│   ├── confirmation.py — 7-слойный движок подтверждения
│   ├── patterns.py     — QM, Fakeout, SR Flip, Compression, MPL
│   ├── killzones.py    — фильтр торговых сессий
│   ├── scoring.py      — финальный скор 0-100, dedup-штраф
│   └── scheduler.py    — планировщик с выравниванием по часам
│
├── Хранение и оценка
│   ├── journal.py      — SQLite persistence + миграции
│   ├── evaluator.py    — трекинг исхода свеча за свечой
│   └── portfolio.py    — виртуальный портфель, PnL, статистика
│
├── Отчётность
│   ├── alerts.py       — Telegram: фото графика + подпись + reply при закрытии
│   ├── charting.py     — Plotly графики (интерактив + PNG)
│   ├── dashboard.py    — ASCII + HTML дашборд портфеля
│   └── daily_report.py — ночной отчёт → Telegram
│
├── Бэктестинг
│   └── backtesting.py  — мультимодельный walk-forward + HTML-отчёт
│
├── autostart/
│   ├── windows_task.ps1
│   └── linux_systemd.sh
│
├── requirements.txt
└── README.md
```

---

## Установка

```bash
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows
.venv\Scripts\activate

pip install -r requirements.txt

# Опционально — PNG-графики для Telegram
pip install kaleido
```

---

## Запуск

```bash
# Одноразовый скан
python main.py

# 30-минутный планировщик (выровнен по часам :00 и :30)
python main.py --loop

# Конкретная пара / таймфрейм
python main.py --symbol BTC/USDT --tf 1h

# Без Plotly-чарта
python main.py --no-chart

# Сводка по DB
python main.py --summary

# HTML-дашборд портфеля
python main.py --dashboard

# Ночной отчёт прямо сейчас
python main.py --report

# Бэктест
python main.py --backtest --days 180
```

---

## Бэктестинг

Walk-forward симуляция на исторических данных. Не пишет в live-DB.

```bash
# Все модели, 180 дней
python backtesting.py --days 180 --html

# Конкретные модели
python backtesting.py --models B,F --days 180 --html

# Узкий тест
python backtesting.py \
  --models F \
  --symbols BTC/USDT,ETH/USDT,DOGE/USDT \
  --timeframes 1h,4h \
  --days 365 \
  --html
```

### Модели стратегий

| Модель | Название | Ключевые условия |
|---|---|---|
| A | Baseline | Текущая live-логика, expired = 0R |
| B | Strict OB | Только свежий нетронутый OB, только первый ретест, без equilibrium |
| C | MTF Classic | Сильный дисплейсмент + качество BOS |
| D | P/D Strict | Long только в discount, short только в premium, min RR 2.5 |
| E | Ultra Selective | Всё из D + max 10 свечей после BOS |
| F | Balanced | Свежий OB + дисплейсмент, equilibrium разрешён |

Добавить новую модель: одна запись `ModelConfig` в `MODEL_REGISTRY` в `backtesting.py`.

**Методология expired:** `expired_treatment = "zero"` — expired всегда 0R. Mark-to-market создаёт фиктивную прибыль и искажает все метрики.

---

## Пайплайн сигнала

```
Fetch OHLCV
  → Свинги (HH / HL / LH / LL)
  → Зоны ликвидности (swing H/L + equal H/L)
  → Sweep (пробой зоны с отклонением)
  → BOS после sweep
  → Order Blocks + FVG
  → 7-слойное подтверждение
      Layer 1  HTF alignment      (25 pts, mandatory)
      Layer 2  Sweep quality      (15 pts, mandatory)
      Layer 3  BOS strength       (20 pts, mandatory)
      Layer 4  OB / FVG presence  (20 pts, mandatory)
      Layer 5  Premium / Discount (10 pts, mandatory)
      Layer 6  Liquidity target   (10 pts)
      Layer 7  Institutional patterns — QM / Fakeout / SR Flip / CP / MPL
  → Entry zone (OB или FVG; FVG-only заблокированы)
  → Score 0-100
  → Сохранение в DB
  → Telegram-алерт с PNG-графиком
```

---

## Подтверждение (confirmation.py)

Семь слоёв последовательно. Каждый mandatory слой **hard-blocks** сигнал при провале.

| Слой | Вес | Mandatory | Что проверяет |
|---|---|---|---|
| 1. HTF alignment | 25 pts | ✅ | HTF тренд совпадает с направлением |
| 2. Sweep quality | 15 pts | ✅ | Доминирование фитиля, быстрое отклонение |
| 3. BOS strength | 20 pts | ✅ | Размер тела свечи, закрытие за структурой, FVG на BOS |
| 4. OB / FVG | 20 pts | ✅ | Нетронутый OB или незакрытый FVG |
| 5. Premium/Discount | 10 pts | ✅ | Правильная зона для направления |
| 6. Liq target | 10 pts | — | Есть ли ликвидность впереди |
| 7. Patterns | ±pts | частично | QM, Fakeout, SR Flip, Compression, MPL |

```python
# config.py — ключевые пороги
CONFIRMATION_MIN_SCORE    = 75    # минимальный балл для сигнала
CONFIRMATION_HTF_MANDATORY   = True
CONFIRMATION_SWEEP_MANDATORY = True
CONFIRMATION_BOS_MANDATORY   = True
CONFIRMATION_OB_MANDATORY    = True
CONFIRMATION_PD_MANDATORY    = True
```

HTF=range = 0 pts (блокируется mandatory-фильтром). Диапазонный HTF не даёт направленного bias.

---

## Институциональные паттерны (patterns.py)

Паттерны из чит-шита CMS (Mansor Sapari). У каждого есть переключатель в `config.py`.

| Паттерн | Флаг | Эффект |
|---|---|---|
| **Quasimodo (QM)** | `USE_QM_FILTER` | +10 pts при подтверждённой структуре HH→HL→HHH |
| **Continuation QM** | `USE_QM_FILTER` | +10 pts — QM по тренду |
| **Ignored QM** | `USE_QM_FILTER` | 0 pts — уровень ранее пробивался, нужно дополнительное подтверждение |
| **Fakeout V1** | `USE_FAKEOUT_FILTER` | −20 pts / hard block — BOS сразу разворачивается обратно |
| **Fakeout V2** | `USE_FAKEOUT_FILTER` | −8 pts — BOS на сильно протестированном S/R |
| **Fakeout V3 / Diamond** | `USE_FAKEOUT_FILTER` | −10 pts — 2 sweep одного уровня, второй слабее |
| **SR Flip** | `USE_SR_FLIP` | +8 pts — уровень BOS ранее был resistance/support |
| **Compression / Flag B** | `USE_COMPRESSION` | +6 pts — сжатие диапазона перед sweep |
| **MPL** | `USE_MPL` | +8/+12 pts — 3+/5+ касаний swept уровня |

**Fakeout V1 — самый важный фильтр.** Бэктест показал что большинство убыточных SHORT-сигналов (14% WR) были fakeout BOS: цена пробивала структуру вниз, мы заходили в SHORT, но 1-2 свечи после BOS цена возвращалась выше → стоп. Этот фильтр блокирует такие входы.

```python
# config.py — паттерны
USE_FAKEOUT_FILTER      = True
FAKEOUT_BLOCK_THRESHOLD = 0.70   # confidence ≥ 70% → hard block
FAKEOUT_V1_PENALTY      = -20
FAKEOUT_V2_PENALTY      = -8
FAKEOUT_V3_PENALTY      = -10

USE_QM_FILTER           = True
QM_STANDARD_BONUS       = 10

USE_SR_FLIP             = True
SR_FLIP_BONUS           = 8

USE_COMPRESSION         = True
COMPRESSION_BONUS       = 6

USE_MPL                 = True
MPL_BONUS               = 8
MPL_STRONG_BONUS        = 12     # 5+ касаний
```

---

## Kill Zones (killzones.py)

Фильтр торговых сессий. По умолчанию логирует но не блокирует.

| Зона | UTC | Качество |
|---|---|---|
| Asian KZ | 00:00–04:00 | ★ |
| London Open | 07:00–09:00 | ★★★ |
| New York | 12:00–14:00 | ★★★ |
| London Close | 15:00–16:00 | ★★ |
| NY PM | 19:00–20:00 | ★ |

```python
KILLZONE_MODE = "log"    # log | filter | score | off
```

- `"log"` — всегда пропускает, пишет KZ-статус в лог (рекомендуется для старта)
- `"filter"` — блокирует сигналы вне KZ
- `"score"` — влияет только на scoring (±pts)

---

## Виртуальный портфель

Симулирует $100 с риском 1% на сделку (compound).

```python
VIRTUAL_BALANCE         = 100.0
RISK_PER_TRADE_PCT      = 0.01

# Трейлинг-стоп к безубытку
TRAILING_STOP_ENABLED   = True
TRAILING_STOP_TRIGGER_R = 1.0    # переместить SL в BE при движении +1R
```

Позиция = `risk_usd / |entry_mid − stop_loss|`

```bash
python main.py --dashboard    # HTML + ASCII дашборд
python main.py --report       # отчёт в стиле ночного
```

---

## Планировщик

Запуск каждые 30 минут по реальным границам часа (`:00`, `:30`). Без дрейфа.

```python
SCAN_INTERVAL_MINUTES = 30
RUN_ON_START          = True
LOCAL_TIMEZONE        = "Europe/Kiev"  # для определения полуночи
```

В 00:00 по локальному времени планировщик автоматически запускает ночной отчёт перед очередным сканом.

---

## Telegram

1. Создать бота через `@BotFather` → `/newbot`
2. Получить chat ID: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Прописать в `config.py`:

```python
TELEGRAM_ENABLED      = True
TELEGRAM_BOT_TOKEN    = "your_token"    # ⚠️ не коммитить в git!
TELEGRAM_CHAT_ID      = "your_chat_id"
ALERT_SCORE_THRESHOLD = 75
```

**Что включают алерты:**
- PNG-график (Plotly, требует `pip install kaleido`)
- Caption: entry / SL / TP / RR / score / HTF / зона
- Reply при закрытии сигнала: исход + MFE + MAE

Dedup: одинаковый symbol/tf/direction в рамках `DEDUP_LOOKBACK_HOURS` не дублируется.

> ⚠️ Никогда не храните токен в git. Используйте `.env` файл или добавьте `config.py` в `.gitignore`.

---

## Журнал сигналов

Хранится в `signals.db` (SQLite, создаётся автоматически).

| Поле | Описание |
|---|---|
| `status` | pending → triggered → won / lost / expired |
| `score` | 0-100 |
| `entry_hit` | цена вошла в зону |
| `mfe` | max favorable excursion от entry |
| `mae` | max adverse excursion от entry |
| `position_size` | размер виртуальной позиции |
| `pnl_usd` | виртуальный P&L |
| `expires_at` | авто-экспирация |
| `signal_hash` | ключ dedup |
| `telegram_message_id` | ID сообщения для reply-threading |

### Статусы

| Статус | Значение |
|---|---|
| `pending` | ждёт входа в зону |
| `triggered` | цена вошла в зону |
| `won` | TP достигнут первым |
| `lost` | SL достигнут первым |
| `expired` | ни SL ни TP до экспирации |

---

## Автозапуск

### Windows
```powershell
# Запустить от имени администратора
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
.\autostart\windows_task.ps1
```

Регистрирует задачу в Task Scheduler: `python main.py --loop --no-chart` при входе в систему.

### Linux
```bash
chmod +x autostart/linux_systemd.sh
./autostart/linux_systemd.sh

# Управление
systemctl --user status smc_scanner
journalctl --user -u smc_scanner -f
```

---

## Префиксы в консоли

```
[SCAN]      — сканирование рынка
[CONF]      — результаты слоёв подтверждения
[PATTERN]   — институциональные паттерны
[KZ]        — проверка kill zone
[SCORE]     — финальный скор
[DB]        — операция с базой данных
[EVAL]      — оценка открытых сигналов
[ALERT]     — Telegram-алерт отправлен
[SKIP]      — дубликат подавлен
[PORTFOLIO] — виртуальная сделка открыта / закрыта
[REPORT]    — ночной отчёт
[SCHEDULER] — информация планировщика
[BT]        — прогресс бэктеста
```

---

## Рекомендованная конфигурация для live

```python
# Символы (проверены бэктестом)
SYMBOLS    = ["BTC/USDT", "ETH/USDT", "DOGE/USDT"]
TIMEFRAMES = ["1h", "4h"]

# Фильтры
CONFIRMATION_MIN_SCORE    = 75
CONFIRMATION_PD_MANDATORY = True
KILLZONE_MODE             = "log"    # → "filter" после наблюдения

# Паттерны
USE_FAKEOUT_FILTER = True   # самый важный
USE_QM_FILTER      = True
USE_MPL            = True
USE_SR_FLIP        = True
USE_COMPRESSION    = True

# Трейлинг-стоп
TRAILING_STOP_ENABLED   = True
TRAILING_STOP_TRIGGER_R = 1.0

# Портфель
VIRTUAL_BALANCE    = 100.0
RISK_PER_TRADE_PCT = 0.01
```

---

## Выводы из бэктестинга

| Находка | Принятое решение |
|---|---|
| 15m таймфрейм: 15% WR | Удалён из TIMEFRAMES по умолчанию |
| FVG-only входы: 0% WR | Заблокированы в signals.py |
| AVAX, LINK, XRP: <15% WR на всех TF | Удалены из SYMBOLS по умолчанию |
| SHORT в equilibrium/discount: 14% WR | P/D mandatory |
| HTF=range: 13% WR | HTF range = 0 pts, блокируется mandatory |
| Score 85-89: 10% WR — хуже score 80-84 | Paradox: высокий скор ≠ лучший исход |
| 31 убыточная сделка с MFE > 1R | Trailing stop к безубытку добавлен |
| SHORT WR низкий несмотря на правильный HTF | Fakeout V1 filter — главная причина |
| 2h и 8h таймфреймы: 10-11% WR | Убраны (нестандартные TF, мало ликвидности) |

---

## Ограничения

- Порядок внутри свечи неизвестен. Если SL и TP оба попадают в одну свечу — результат определяется по close (консервативно).
- Данные биржи публичные. API-ключ не нужен для Binance OHLCV.
- HTF fetch — дополнительный API-вызов на каждый сигнал.
- Для статистически значимого бэктеста нужно минимум 50 decisive trades.

---

## Дисклеймер

Только для образовательных и исследовательских целей. Не размещает реальных ордеров.  
Прошлые результаты не гарантируют будущих.  
Всегда применяйте собственный риск-менеджмент перед использованием любого сигнала.