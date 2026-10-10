---
name: check-data-quality
description: Check data quality of stored scanner data — missing 1h candles, zero-volume candles, CoinGecko vs Binance ATL divergence, mapping and age statuses. Use before trusting screen results or when metrics look suspicious.
---

# check-data-quality

Основной путь — команда (только читает БД, пороги в секции `dq` конфига):
```powershell
uv run atl-scanner dq -c config.yaml
```
Пишет `data/reports/dq-<UTC>.md`. Для деталей по конкретной проверке — SQL-запросы ниже к
`data/scanner.duckdb` (`read_only=True`) или `atl_scanner.quality.<check>(...).details`.

## Проверки
1. **Пропуски 1h-свечей** — ожидаемое число свечей между первой и последней vs фактическое:
   ```sql
   select symbol, min(open_time) first_ts, max(open_time) last_ts, count(*) n,
          datediff('hour', min(open_time), max(open_time)) + 1 - count(*) as missing
   from ohlcv_1h group by symbol having missing > 0 order by missing desc;
   ```
   Отдельно: самые длинные дыры (`open_time - lag(open_time) > interval 1 hour`).
2. **Нулевой объём / вырожденные свечи**: `volume = 0`, `high = low`, `low <= 0`,
   `low > least(open, close)` или `high < greatest(open, close)`. Доля на символ.
3. **Расхождение ATL**: `atl_divergence_log` из `screen_results` последнего прогона.
   - `< 0` — Binance-минимум ниже глобального ATL CoinGecko: подозрение на ошибочную свечу
     или неверное сопоставление символа → проверить вручную.
   - большое `> 0` — ожидаемо (ATL был раньше года назад или на другой бирже); не ошибка.
4. **Сопоставление**: количество `no_pair` / `ambiguous` / `manual`; список `ambiguous` для ручных overrides.
5. **Возраст**: количество `age_status = unknown`; у `binance_first_candle` проверить, что
   первая свеча действительно ≥ `min_age_days` назад.
6. **Свежесть**: последняя свеча каждого символа не старше 2 ч; `cg_last_updated` не старше
   TTL кэша; монеты с остановленной торговлей.
7. **Исключения**: распределение `exclusion_reason`; глазами проверить топ-20 по капитализации
   на пропущенные стейблкоины/wrapped.

## Как докладывать
Таблица: проверка → число затронутых монет → примеры (≤ 5 символов) → что предлагается
(override в конфиге, перекачка свечей, исключение). Ничего не удалять из БД и `data/raw/`
без подтверждения пользователя. Не делать выводов о рыночном поведении монет.
