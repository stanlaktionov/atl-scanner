# ARCHITECTURE — Crypto ATL Scanner (MVP, вариант A)

Локальный скринер: находит монеты, которые подходят для **исследования** гипотезы
`STABLE → PUMP → DUMP → STABLE → PUMP → FINAL DUMP`. Гипотеза не проверена;
скринер её не использует и не проверяет. Он только отбирает кандидатов
(капитализация, возраст, близость к ATL) и сохраняет метрики для дальнейшего анализа.

## 1. Компоненты и поток данных

```
             ┌────────────────────── sources/ (ЕДИНСТВЕННОЕ место с сетью) ──────────────────────┐
             │  http.py: httpx client + retries + rate limiter + raw disk cache                   │
             │  coingecko.py: /coins/markets, категории исключений                                │
             │  binance.py:   /api/v3/exchangeInfo, /api/v3/klines                                │
             └───────────────┬────────────────────────────────────────────────────────────────────┘
                             │ сырые ответы → data/raw/<source>/<endpoint>/<date>/<key>_<time>.json.gz
                             ▼
  fetch ──► normalize/ ──► store/ (DuckDB) ──► metrics/ ──► screen.py ──► report.py
            pandas DF      coins,              ATR, dist_*   фильтры из     CSV + Markdown
            типы, UTC,     market_snapshots,   new-low,      конфига,       (+ атрибуция
            единицы        ohlcv_1h            расхождение   screen_results  CoinGecko)
                                               ATL
```

| Шаг | Модуль | Вход | Выход | Сеть |
|---|---|---|---|---|
| fetch | `sources/` | конфиг, `.env` | сырые JSON на диске + python-объекты | да |
| normalize | `normalize/`, `mapping.py`, `exclusions.py` | сырые объекты | `pandas.DataFrame` со строгими колонками | нет |
| store | `store/` | DataFrame | таблицы DuckDB (идемпотентный upsert) | нет |
| screen | `metrics/`, `screen.py` | таблицы | `atl_metrics`, `screen_results` | нет |
| report | `report.py` | `screen_results` | `data/reports/<run_id>.{csv,md}` | нет |

Оркестрация — `pipeline.py`; CLI (`cli.py`, typer) — тонкая обёртка над pipeline.

**Хранилище: DuckDB** — встраиваемая колоночная БД в одном файле, быстрые аналитические
запросы по OHLCV и нативный обмен с pandas, без сервера.

**HTTP: httpx напрямую, без ccxt** — нужны всего два эндпоинта Binance, а прямой
доступ даёт контроль над весами запросов (`X-MBX-USED-WEIGHT-1M`) и кэшем сырых ответов.

## 2. Ключевые решения

### 2.1 Вселенная монет
1. `GET /coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=N`.
   Пагинация останавливается, как только последняя монета на странице ниже
   `universe.min_market_cap_usd` (сортировка по капитализации делает это корректным).
2. Исключения (`exclusions.py`), каждое с записанной причиной `exclusion_reason`:
   - категории CoinGecko из конфига (`stablecoins`, `wrapped-tokens`, `bridged-tokens`, …)
     через тот же `/coins/markets?category=<id>` с той же остановкой по капитализации;
   - ручные списки `ids` / `symbols` из конфига;
   - regex по имени (`name_patterns`) — страховка для неразмеченных монет.
   Исключённые монеты **сохраняются** в `coins` с флагом, а не удаляются.
   `exclusions.keep_ids` — белый список: монета остаётся в выборке, причины всё равно пишутся
   (нужно, т.к. категории CoinGecko неточны: напр. `spx6900` числится в `bridged-tokens`).
3. Наблюдение (2026-10-10): основной `/coins/markets` сам не отдаёт крупные wrapped/bridged-токены
   (WBTC, wstETH, WETH…) — они есть только в выдаче категорий. Исключения по категориям — страховка.

### 2.2 Сопоставление CoinGecko ↔ Binance (`mapping.py`)
- `exchangeInfo` (вес 20, кэш 24 ч) → пары со `status=TRADING`, разрешённой спот-торговлей и
  `quoteAsset ∈ mapping.quote_assets` (по приоритету, по умолчанию `USDT`); `ticker/price` (вес 4)
  — текущие цены всех пар.
- Совпадение по `upper(cg.symbol) == baseAsset` либо через префикс из
  `mapping.base_prefix_multipliers` (`1000SATS` = цена за 1000 SATS → `binance_price_multiplier = 1000`).
  Прямая пара предпочитается префиксной.
- Проверка цены: `binance_price_dev_log = |ln(цена Binance / множитель / цена CoinGecko)|`;
  выше `mapping.max_price_deviation_log` — вероятно другой токен с тем же тикером.
  Это проверка данных, а не порог скрининга.
- `mapping_status`: `mapped` | `no_pair` | `price_mismatch` | `ambiguous` (несколько монет
  проходят проверку цены — пару держит монета с ближайшей ценой, при равенстве — с большей
  капитализацией; помечаются все) | `manual` (из `mapping.overrides`, имеют приоритет).
  Монеты без пары остаются в выдаче с пометкой.

### 2.3 Возраст > 1 года (`age_status`)
Критерий: есть цена/свеча ≥ `universe.min_age_days` дней назад. Источники доказательства, по порядку:
1. `binance_first_candle` — первая 1h-свеча в окне истории открыта ≥ N дней назад.
2. `coingecko_atl_or_ath_date` — `atl_date` или `ath_date` старше N дней (монета тогда уже торговалась).
3. иначе `unknown` (не `false`!). Опционально: `/coins/{id}/market_chart?days=365`
   с жёстким лимитом вызовов (`sources.coingecko.age_fallback_max_calls`, по умолчанию 0).

### 2.4 Две ATL и метрики (`metrics/`)
Для каждой монеты считаются строки по двум источникам ATL (`atl_source`):

| atl_source | ATL | цена | охват |
|---|---|---|---|
| `coingecko_all_time` | `atl` из `/coins/markets` | `current_price` CoinGecko (USD) | вся история, все биржи |
| `binance_1y` | минимум `metrics.binance_atl_price` (по умолчанию `close`) по 1h-свечам | последний `close` Binance (USDT) | ≤ 1 год, одна пара |

Метрики (все пороги/окна — из конфига):
- `dist_pct = (price − ATL) / ATL`
- `dist_log = ln(price) − ln(ATL)`
- `dist_atr = (price − ATL) / ATR(N)`, ATR по Уайлдеру на 1h-свечах Binance
  (для CoinGecko-строки ATR тот же, допущение USDT≈USD — см. ограничения)
- `is_new_low_in_window` — минимум `low` за последние `metrics.new_low_window_days`
  равен минимуму за всё окно Binance; плюс `days_since_atl`
- расхождение источников: `atl_divergence_log = ln(ATL_binance_1y) − ln(ATL_coingecko)`
  (≥ 0 ожидаемо: годовой минимум одной биржи не ниже глобального исторического)

2h-свечи не хранятся отдельно: получаются ресемплингом 1h (`normalize/binance.py`),
это экономит запросы. Порог «около ATL» (`screen.near_atl.max`) **не задан по умолчанию**:
без него скринер ранжирует, а не фильтрует.

## 3. Схема таблиц (DuckDB, `data/scanner.duckdb`)

Все временные метки — `TIMESTAMP` в UTC. Цены — `DOUBLE`.

```sql
-- запуск скана: для воспроизводимости
runs(
  run_id           VARCHAR PRIMARY KEY,   -- 20261010T130023885Z-e51f3a: UTC до мс + хэш конфига
  started_at       TIMESTAMP,
  finished_at      TIMESTAMP,
  config_hash      VARCHAR,
  config_yaml      VARCHAR,               -- снимок конфига целиком
  cg_calls         INTEGER,
  binance_weight   INTEGER,
  status           VARCHAR,               -- running | ok | failed
  snapshot_run_id  VARCHAR                -- для screen: fetch-прогон, чьи данные отобраны
)

coins(
  cg_id            VARCHAR PRIMARY KEY,
  symbol           VARCHAR,
  name             VARCHAR,
  categories       VARCHAR[],             -- только те, что проверялись для исключений
  excluded         BOOLEAN,
  exclusion_reason VARCHAR,               -- category:stablecoins | manual_id | name_pattern:... | NULL
  binance_symbol   VARCHAR,               -- напр. 'ABCUSDT' или NULL
  binance_price_multiplier INTEGER,       -- 1, 1000 (1000SATS), 1000000 (1MBABYDOGE)
  mapping_status   VARCHAR,               -- mapped | no_pair | price_mismatch | ambiguous | manual
  binance_price_dev_log    DOUBLE,        -- |ln(Binance/CoinGecko price)| на момент сопоставления
  first_seen_at    TIMESTAMP,
  updated_at       TIMESTAMP
)

market_snapshots(
  run_id               VARCHAR,
  cg_id                VARCHAR,
  snapshot_ts          TIMESTAMP,
  price_usd            DOUBLE,
  market_cap_usd       DOUBLE,
  market_cap_rank      INTEGER,
  total_volume_usd     DOUBLE,
  ath                  DOUBLE,
  ath_date             TIMESTAMP,
  atl                  DOUBLE,
  atl_date             TIMESTAMP,
  atl_change_percentage DOUBLE,
  cg_last_updated      TIMESTAMP,
  PRIMARY KEY (run_id, cg_id)
)

ohlcv_1h(
  exchange      VARCHAR,                  -- 'binance'
  symbol        VARCHAR,                  -- 'ABCUSDT'
  open_time     TIMESTAMP,
  open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
  volume        DOUBLE,                   -- base asset
  quote_volume  DOUBLE,
  trades        INTEGER,
  PRIMARY KEY (exchange, symbol, open_time)
)

atl_metrics(
  run_id               VARCHAR,
  cg_id                VARCHAR,
  atl_source           VARCHAR,           -- coingecko_all_time | binance_1y
  atl_price            DOUBLE,
  atl_ts               TIMESTAMP,
  price                DOUBLE,
  price_ts             TIMESTAMP,
  dist_pct             DOUBLE,
  dist_log             DOUBLE,
  atr_period           INTEGER,
  atr_value            DOUBLE,
  dist_atr             DOUBLE,
  new_low_window_days  INTEGER,
  is_new_low_in_window BOOLEAN,
  days_since_atl       DOUBLE,
  PRIMARY KEY (run_id, cg_id, atl_source)
)

screen_results(
  run_id               VARCHAR,
  cg_id                VARCHAR,
  symbol               VARCHAR,
  binance_symbol       VARCHAR,
  mapping_status       VARCHAR,
  market_cap_usd       DOUBLE,
  age_status           VARCHAR,           -- binance_first_candle | coingecko_atl_or_ath_date | unknown
  first_candle_ts      TIMESTAMP,
  excluded             BOOLEAN,
  exclusion_reason     VARCHAR,
  -- метрики по обоим источникам, денормализовано для удобства отчёта
  cg_dist_pct DOUBLE, cg_dist_log DOUBLE, cg_dist_atr DOUBLE,
  bn_dist_pct DOUBLE, bn_dist_log DOUBLE, bn_dist_atr DOUBLE,
  bn_is_new_low_in_window BOOLEAN,
  atl_divergence_log   DOUBLE,
  passes_mcap          BOOLEAN,
  passes_age           BOOLEAN,           -- NULL, если age_status = unknown
  passes_near_atl      BOOLEAN,           -- NULL, если порог не задан
  passes_all           BOOLEAN,
  PRIMARY KEY (run_id, cg_id)
)
```

## 4. Бюджет запросов на один прогон

### CoinGecko Demo (10 000 вызовов/мес, 100/мин с мая 2026; лимитер на 25/мин — узкое место месячная квота)

| Запрос | Вызовов | Комментарий |
|---|---|---|
| `/coins/markets` (вселенная, 250/стр) | ~4–8 | остановка по `min_market_cap_usd`; факт 2026-10-10: 4 стр., 905 монет > $20M |
| `/coins/markets?category=…` (исключения) | ~1–2 на категорию × 3–5 | кэш на диск, TTL 24 ч |
| `/coins/categories/list` | 0–1 | проверка id категорий, кэш 7 дней |
| `/coins/{id}/market_chart` (fallback возраста) | 0 … `age_fallback_max_calls` | по умолчанию выключен |
| **Итого** | **≈ 10–20** (факт: 8) | ≈ 500+ прогонов/мес в рамках лимита |

Каждый прогон пишет фактическое число вызовов в `runs.cg_calls`; при превышении
`sources.coingecko.max_calls_per_run` прогон останавливается с понятной ошибкой.

### Binance (без ключа, лимит весов 6000/мин на IP; лимитер на 50 %)

| Запрос | Вес | Количество |
|---|---|---|
| `exchangeInfo` | 20 | 1 (кэш 24 ч) |
| `klines` 1h, `limit=1000` | 2 | первичная загрузка: ~9 на монету (8760 свечей/год); инкремент: 1 на монету |

Факт первой загрузки (2026-10-10): 321 пара, ~2 700 запросов, вес 5 456, 15 мин.
Узкое место — задержка ~1 с на запрос, а не лимит весов, поэтому свечи качаются
параллельно (`sources.binance.max_workers`, по умолчанию 6). Результат: 2.64 млн свечей,
у 279 пар полный год; DuckDB 189 МБ, сырые ответы (gzip) 119 МБ. Последующие прогоны —
1 запрос на пару, если закрылся новый час. Свечи старше окна `history_days` удаляются.
Фактический вес читается из заголовка `X-MBX-USED-WEIGHT-1M`.

## 5. Известные ограничения данных

1. **Разный охват ATL.** CoinGecko — вся история по всем биржам; Binance — ≤ 1 год, одна
   пара, с момента листинга. Монета может быть «у годового минимума Binance» и далеко от
   глобального ATL. Поэтому хранятся обе и показывается расхождение.
2. **Demo API отдаёт историю только за 365 дней**; поле `atl` — единственный источник
   «всей истории», пересчитать его нельзя. Ошибочные принты в ATL CoinGecko не отфильтровать.
3. **USDT ≠ USD.** Binance-цены в USDT; сравнение с USD-ценами CoinGecko предполагает
   паритет. Во время депега метрики искажаются.
4. **Сопоставление по символу ненадёжно:** коллизии тикеров, ребрендинги, редоминации
   (`1000XXX`, `LUNA`/`LUNC`), разные токены с одним тикером. Частично ловится проверкой
   цены и префиксами-множителями; остальное — `mapping.overrides`. Первый прогон
   (2026-10-10): из 1000 монет 349 mapped, 645 no_pair, 4 price_mismatch, 2 ambiguous;
   у mapped медиана отклонения цены 0.15 %, максимум 5.8 %.
5. **Systematic bias выборки (survivorship):** `/coins/markets` возвращает только живые
   монеты; делистинговые и умершие не попадут в выборку.
6. **Пропуски свечей** (техработы, приостановка торгов) и свечи с нулевым объёмом
   искажают ATR и минимумы — проверяются `atl-scanner dq`.
7. **Задержки обновления:** `current_price`/`atl` CoinGecko обновляются с лагом
   (`cg_last_updated`), свечи Binance — почти в реальном времени; цены на один момент не совпадают.
8. **Гео-ограничения Binance** (HTTP 451 в ряде стран). По умолчанию используется
   market-data-зеркало `https://data-api.binance.vision`; базовый URL — в конфиге.
9. **Возраст «unknown»** для монет без пары на Binance и со свежими ATL/ATH — это не «молодая монета».
10. Метки категорий CoinGecko неполные: часть wrapped/bridged-токенов не размечена.

11. **Тени свечей.** Обвал 2025-10-10 21:00 UTC оставил на многих парах Binance `low` около нуля (ATOM: low 0.001 при close 2.944). С ATL по `low` у 90 из 321 монеты годовой минимум Binance оказывался ниже исторического ATL CoinGecko; с `close` — у 3. Поэтому по умолчанию `close`.
12. **Токенизированные реальные активы** (фонды, облигации, кредиты, акции, золото) почти не меняются в цене и всегда «на ATL». Исключаются узким списком категорий CoinGecko, но часть таких токенов не размечена (2026-10-10: VanEck Treasury Fund, Spiko, Fidelity Digital Interest Token и др.) — их ловить вручную через `exclusions.ids`.

## 6. Атрибуция
Каждый отчёт (`.md`, CLI-вывод, Streamlit) содержит строку **«Data provided by CoinGecko»**
со ссылкой на https://www.coingecko.com.
