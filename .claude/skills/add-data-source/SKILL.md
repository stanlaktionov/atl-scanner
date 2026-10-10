---
name: add-data-source
description: Add a new exchange or data provider (e.g. Bybit, OKX) through the common source interface in sources/base.py. Use when asked to support another exchange, candle source or market list provider.
---

# add-data-source

## Правила
- Сетевой код — **только** в `src/atl_scanner/sources/<name>.py`.
- Источник реализует Protocol из `sources/base.py`: `CandleSource` (свечи) или
  `MarketListSource` (список монет + капитализация + ATL). Новый метод в Protocol — только
  если без него нельзя, и с обновлением всех реализаций.
- Используется общий клиент `sources/http.py` (ретраи, `Retry-After`), лимитер из
  `sources/ratelimit.py` и raw-кэш `sources/cache.py` — свой httpx-клиент не создавать.
- История не глубже 1 года, таймфрейм не меньше 1h (минутные данные вне скоупа).

## Шаги
1. **Изучить API**: эндпоинты, формат свечей, лимиты (вызовы или веса), пагинация, гео-ограничения.
   Записать в docstring модуля.
2. **Конфиг**: секция `sources.<name>` в pydantic-модели (`config.py`) и в `config.example.yaml`:
   `base_url`, лимиты, `klines_limit`, `history_days`, `cache_ttl_hours`. Без значений в коде.
3. **Источник** `sources/<name>.py`: класс с атрибутом `name`, методы Protocol; возвращает
   сырые данные и пишет их в `data/raw/<name>/...`.
4. **Нормализация** `normalize/<name>.py`: сырые данные → DataFrame со схемой `ohlcv_1h`
   (`exchange='<name>'`, UTC, float). Свечи хранятся в общей таблице, различаются колонкой `exchange`.
5. **Mapping**: расширить `mapping.py` (символ биржи ↔ `cg_id`), статусы те же;
   приоритет бирж — порядок в конфиге.
6. **Тесты** (без сети): записать 1–2 реальных ответа в `tests/fixtures/<name>/`, тесты через
   `httpx.MockTransport`: пагинация, 429 → ретрай, нормализация в схему, mapping.
   Сетевой smoke-тест — с `@pytest.mark.network`.
7. **Документация**: ARCHITECTURE.md (бюджет запросов, ограничения данных нового источника),
   строка в таблице компонентов.

## Чек-лист готовности
- [ ] `uv run pytest`, `uv run mypy src`, `uv run ruff check .` зелёные
- [ ] в коде нет URL/лимитов/порогов — только конфиг
- [ ] сырые ответы в `data/raw/<name>/`, нормализованные — только в DuckDB
- [ ] монеты без пары помечены, не выброшены
