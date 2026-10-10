---
name: run-scan
description: Run the ATL scanner (full scan or a single stage) with config parameters and find the results. Use when asked to run a scan, rerun with different thresholds, or look at the latest screen results.
---

# run-scan

## Перед запуском
1. `config.yaml` и `.env` существуют (иначе скопировать из `*.example`, ключ `COINGECKO_API_KEY` заполнен).
2. Проверить бюджет: последний `runs.cg_calls` и сколько прогонов уже было в этом месяце
   (Demo ≈ 10 000 вызовов/мес). Не запускать полный `scan` в цикле ради отладки.
3. Пороги менять **только в `config.yaml`** (или во временной копии), не в коде.

## Команды
```powershell
uv run atl-scanner scan   -c config.yaml   # полный прогон, сеть
uv run atl-scanner fetch  -c config.yaml   # загрузка: CoinGecko + mapping + 1h-свечи Binance
                                           #   первая загрузка ~10 мин, дальше докачка хвоста
uv run atl-scanner screen -c config.yaml   # пересчёт метрик/фильтров по БД, без сети
```
Подобрать другой порог «около ATL» — отредактировать `screen.near_atl` в копии конфига
и запустить `screen` (сеть не нужна):
```powershell
copy config.yaml data\config.try.yaml   # правка: screen.near_atl.max / metric / atl_source
uv run atl-scanner screen -c data\config.try.yaml
```

## Где результат
- Отчёт: `data/reports/<run_id>.csv` и `.md` (последний — по времени изменения).
- БД: `data/scanner.duckdb`, таблицы `runs`, `screen_results`, `atl_metrics`.
```powershell
uv run python -c "import duckdb; c=duckdb.connect('data/scanner.duckdb', read_only=True); print(c.sql('select * from runs order by started_at desc limit 5'))"
uv run python -c "import duckdb; c=duckdb.connect('data/scanner.duckdb', read_only=True); print(c.sql(\"select symbol, market_cap_usd, cg_dist_log, bn_dist_log, atl_divergence_log, mapping_status, age_status, passes_all from screen_results where run_id=(select max(run_id) from runs) order by cg_dist_log limit 30\"))"
```

## Как докладывать результат
- run_id, число монет на каждом этапе (вселенная → после исключений → mapped → прошли фильтры),
  фактические `cg_calls` и `binance_weight`.
- Отдельно: сколько `no_pair`, `ambiguous`, `age_status=unknown`.
- Не интерпретировать выборку как сигналы и не добавлять торговых выводов.
- Упомянуть «Data provided by CoinGecko», если показываешь данные.
