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
                             │ сырые ответы (JSON) → data/raw/<source>/<endpoint>/<date>/<hash>.json
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

### 2.2 Сопоставление CoinGecko ↔ Binance (`mapping.py`)
- `exchangeInfo` → пары со `status=TRADING` и `quoteAsset ∈ mapping.quote_assets` (по умолчанию `USDT`).
- Совпадение по `upper(cg.symbol) == baseAsset`.
- `mapping_status`: `mapped` | `no_pair` | `ambiguous` (несколько CG id на один символ —
  пара достаётся монете с наибольшей капитализацией, остальные помечаются) | `manual`
  (из `mapping.overrides`). Монеты без пары остаются в выдаче с пометкой.

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
| `binance_1y` | `min(low)` по 1h-свечам | последний `close` Binance (USDT) | ≤ 1 год, одна пара |

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
  run_id           VARCHAR PRIMARY KEY,   -- UTC ISO + короткий хэш
  started_at       TIMESTAMP,
  finished_at      TIMESTAMP,
  config_hash      VARCHAR,
  config_yaml      VARCHAR,               -- снимок конфига целиком
  cg_calls         INTEGER,
  binance_weight   INTEGER,
  status           VARCHAR                -- ok | partial | failed
)

coins(
  cg_id            VARCHAR PRIMARY KEY,
  symbol           VARCHAR,
  name             VARCHAR,
  categories       VARCHAR[],             -- только те, что проверялись для исключений
  excluded         BOOLEAN,
  exclusion_reason VARCHAR,               -- category:stablecoins | manual_id | name_pattern:... | NULL
  binance_symbol   VARCHAR,               -- напр. 'ABCUSDT' или NULL
  mapping_status   VARCHAR,               -- mapped | no_pair | ambiguous | manual
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

### CoinGecko Demo (~10 000 вызовов/мес, ~30/мин; лимитер ставим на 25/мин)

| Запрос | Вызовов | Комментарий |
|---|---|---|
| `/coins/markets` (вселенная, 250/стр) | ~4–8 | остановка по `min_market_cap_usd`; число монет > $20M заранее неизвестно |
| `/coins/markets?category=…` (исключения) | ~1–2 на категорию × 3–5 | кэш на диск, TTL 24 ч |
| `/coins/categories/list` | 0–1 | проверка id категорий, кэш 7 дней |
| `/coins/{id}/market_chart` (fallback возраста) | 0 … `age_fallback_max_calls` | по умолчанию выключен |
| **Итого** | **≈ 10–20** | ≈ 500+ прогонов/мес в рамках лимита |

Каждый прогон пишет фактическое число вызовов в `runs.cg_calls`; при превышении
`sources.coingecko.max_calls_per_run` прогон останавливается с понятной ошибкой.

### Binance (без ключа, лимит весов 6000/мин на IP; лимитер на 50 %)

| Запрос | Вес | Количество |
|---|---|---|
| `exchangeInfo` | 20 | 1 (кэш 24 ч) |
| `klines` 1h, `limit=1000` | 2 | первичная загрузка: ~9 на монету (8760 свечей/год); инкремент: 1 на монету |

Пример: 500 сопоставленных монет → первичная загрузка ≈ 4 500 запросов / 9 000 весов
(≈ 3–4 мин при лимитере), последующие прогоны ≈ 500 запросов. Фактический вес
читается из заголовка `X-MBX-USED-WEIGHT-1M`.

## 5. Известные ограничения данных

1. **Разный охват ATL.** CoinGecko — вся история по всем биржам; Binance — ≤ 1 год, одна
   пара, с момента листинга. Монета может быть «у годового минимума Binance» и далеко от
   глобального ATL. Поэтому хранятся обе и показывается расхождение.
2. **Demo API отдаёт историю только за 365 дней**; поле `atl` — единственный источник
   «всей истории», пересчитать его нельзя. Ошибочные принты в ATL CoinGecko не отфильтровать.
3. **USDT ≠ USD.** Binance-цены в USDT; сравнение с USD-ценами CoinGecko предполагает
   паритет. Во время депега метрики искажаются.
4. **Сопоставление по символу ненадёжно:** коллизии тикеров, ребрендинги, редоминации
   (`1000XXX`, `LUNA`/`LUNC`), разные токены с одним тикером. Лечится `mapping.overrides`.
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

## 6. Атрибуция
Каждый отчёт (`.md`, CLI-вывод, Streamlit) содержит строку **«Data provided by CoinGecko»**
со ссылкой на https://www.coingecko.com.
