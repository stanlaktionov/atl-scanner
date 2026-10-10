# Crypto ATL Scanner

Локальный скринер монет около ATL (market cap > порога, возраст > 1 года) — материал для
исследования, не торговые сигналы. Установка и команды — в [CLAUDE.md](CLAUDE.md),
устройство — в [ARCHITECTURE.md](ARCHITECTURE.md), план — в [PLAN.md](PLAN.md).

## Быстрый старт (Windows, PowerShell)

```powershell
cd C:\projects\pump-and-dump
python -m uv sync --extra ui      # зависимости + веб-страница
.venv\Scripts\Activate.ps1        # один раз за сессию
copy config.example.yaml config.yaml
copy .env.example .env            # вписать COINGECKO_API_KEY

atl-scanner ui                    # веб-страница в браузере (localhost:8501)
# или в терминале:
atl-scanner scan                  # загрузка + отбор + отчёт в data/reports/
atl-scanner coin atom             # карточка одной монеты
atl-scanner dq                    # проверка качества данных
```

Data provided by [CoinGecko](https://www.coingecko.com).
