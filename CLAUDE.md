# CLAUDE.md — Crypto ATL Scanner

## Цель
Локальный скринер криптовалют: отбирает монеты с капитализацией выше порога, возрастом
> 1 года и ценой около ATL — как **материал для исследования** гипотезы
`STABLE → PUMP → DUMP → STABLE → PUMP → FINAL DUMP`.

### Ограничение: не добавлять гипотез
- Гипотеза **не проверена**. Не трактовать её как рыночный закон, не писать код/доки,
  которые её «подтверждают».
- Не добавлять от себя торговых гипотез и сигналов: объём как предвестник пампа,
  соцсети, sentiment, обязательность второго пампа, buy/sell-сигналы, скоринг «перспективности».
- Такие идеи — только в `PLAN.md` → «Идеи на потом» с пометкой *(гипотеза, не проверено)*.
- Вне скоупа MVP: серверы, Docker, облако, БД-серверы, минутные данные, история > 1 года,
  ML, бэктесты, алерты, детекция PUMP/DUMP.

Подробно: [ARCHITECTURE.md](ARCHITECTURE.md). План: [PLAN.md](PLAN.md).

## Команды
```powershell
# uv установлен через `pip install --user uv`; его Scripts-папка не в PATH,
# поэтому либо `python -m uv ...`, либо добавить в PATH
# %APPDATA%\Python\Python312\Scripts — тогда работает просто `uv ...`.

uv sync                             # зависимости + dev-группа
uv sync --extra ui                  # + Streamlit (опционально)
copy config.example.yaml config.yaml
copy .env.example .env              # вписать COINGECKO_API_KEY

uv run pytest                       # тесты (сетевые помечены @pytest.mark.network и выключены)
uv run pytest -m network            # только сетевые, вручную
uv run ruff check . ; uv run ruff format .
uv run mypy src

uv run atl-scanner check-config -c config.yaml     # проверить конфиг, показать ключевые пороги
uv run atl-scanner scan --config config.yaml       # полный прогон fetch→report
uv run atl-scanner fetch --config config.yaml      # загрузка: CoinGecko, исключения, mapping, 1h-свечи
uv run atl-scanner fetch --no-candles             # то же без свечей (быстро обновить mapping)
uv run atl-scanner fetch --only coingecko         # только CoinGecko + исключения
uv run atl-scanner screen --config config.yaml     # метрики + фильтры по данным в БД, без сети
uv run atl-scanner screen --top 30                 # показать больше монет, прошедших фильтры
uv run atl-scanner dq --config config.yaml         # проверка качества данных
uv run atl-scanner coin atom                       # карточка одной монеты: id, тикер, пара или имя
```

## Структура
```
config.example.yaml      # ВСЕ пороги, окна, исключения, лимиты (шаблон; config.yaml — локальный, в .gitignore)
.env.example             # COINGECKO_API_KEY
src/atl_scanner/
  cli.py                 # typer: тонкая обёртка над pipeline
  config.py              # загрузка/валидация YAML (pydantic) + .env
  models.py              # dataclass/enum-типы домена (AtlSource, MappingStatus, AgeStatus…)
  pipeline.py            # оркестрация fetch → normalize → store → screen → report
  sources/               # ЕДИНСТВЕННОЕ место с сетевыми вызовами
    base.py              #   Protocol-интерфейсы источников
    http.py              #   httpx-клиент: ретраи, учёт rate limit
    ratelimit.py         #   лимитеры (вызовы/мин, веса/мин)
    cache.py             #   кэш сырых ответов на диск
    coingecko.py, binance.py
  normalize/             # сырые ответы → DataFrame со строгими колонками (UTC, float)
  mapping.py             # CoinGecko id ↔ Binance symbol
  exclusions.py          # стейблкоины, wrapped/bridged, ручные списки
  store/db.py            # DuckDB: схема, upsert, чтение
  metrics/indicators.py  # ATR и прочие чистые индикаторы
  metrics/atl.py         # dist_pct, dist_log, dist_atr, new-low, расхождение ATL
  screen.py              # применение порогов конфига → screen_results
  report.py              # CSV/Markdown + атрибуция CoinGecko
  quality.py             # проверки качества данных
tests/                   # pytest; fixtures/ — записанные сырые ответы API
data/                    # (gitignored) raw/, scanner.duckdb, reports/
.claude/skills/          # run-scan, add-data-source, check-data-quality
```

## Правила кода
- Python 3.11+, полные аннотации типов; `mypy --strict` для `src/` должен проходить.
- Маленькие функции с одной задачей; расчётные функции — чистые (DataFrame/числа на входе и выходе, без I/O).
- **Никакого хардкода порогов, окон, URL, лимитов.** Всё берётся из `config.yaml` через
  `config.py`. Новый параметр = поле в pydantic-модели + запись в `config.example.yaml` с комментарием.
- Значение по умолчанию для порога «около ATL» **не задаётся** и не называется «оптимальным».
- **Сетевые вызовы — только в `src/atl_scanner/sources/`.** Остальные модули не импортируют httpx.
- Все времена — UTC (`tz-aware`), ATL/цены — `float`, деление на ноль/ATL ≤ 0 → `NaN`, а не исключение.
- Новый источник данных — по интерфейсу из `sources/base.py` (см. skill `add-data-source`).
- **Тесты на метрики ATL обязательны**: при любом изменении `metrics/` — тесты с ручным
  расчётом ожидаемых значений, крайние случаи (price == ATL, ATL ≤ 0, NaN, история короче окна ATR).
- Тесты не ходят в сеть: используют фикстуры из `tests/fixtures/` и `httpx.MockTransport`.
- Комментарии и docstring — на английском; документация для людей — на русском.

## Правила для данных
- Не коммитить `.env`, `config.yaml`, `data/` (уже в `.gitignore`). Ключ API — только в `.env`.
- Уважать rate limits: CoinGecko ≤ 25 вызовов/мин (лимит Demo 100/мин, но квота 10k/мес) и `max_calls_per_run`; Binance — по весам,
  читать `X-MBX-USED-WEIGHT-1M`, на 429/418 — остановиться/ждать `Retry-After`.
- Перед сетевым запросом проверять дисковый кэш (TTL из конфига). Не гонять полные загрузки в цикле при отладке.
- **Сырые ответы хранятся отдельно от нормализованных:** `data/raw/<source>/<endpoint>/<date>/<key>_<time>.json.gz` (gzip, ~5–8× меньше)
  неизменяемы; нормализованные данные — только в DuckDB. Нормализация должна воспроизводиться из raw.
- Монеты не выбрасываются молча: исключённые/несопоставленные остаются в таблицах с причиной.
- ATL CoinGecko (вся история) и ATL Binance (≤ 1 год) — разные величины; хранить обе, не смешивать.
- Каждый отчёт содержит «Data provided by CoinGecko».
