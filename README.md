# Aksjeopplysningne

Lite personlig prosjekt for momentum-screening av aksjer.

## Repository boundary

Aksjeopplysningne is a discretionary momentum-screening and trading-assistant tool. It does not perform live trading, broker execution, autonomous position management, or AI trader shadow/champion model switching. Those belong in the separate AI trader project.

## Hva det gjør

- Leser tickere fra `watchlist.csv` eller henter live fra Yahoo Finance screeners
- Henter intradag-data via Yahoo (`yfinance`) eller valgfritt Alpaca for USA (kun data, ingen trading)
- Regner ut momentum-score (0-100-ish) basert på enkle regler
- Beriker hver ticker med sektor/industri/thematic tags, market-cap-tier, float-risk, ATR%, premarket gap/volume og earnings-nærhet
- Lager enkel catalyst-hook (nyhetsheadlines + sentiment-tag) og insider-placeholder
- Deler tickere i:
  - `A-list` (score >= 70)
  - `B-list` (score 45-70)
  - `C-list` (score < 45)
- Lager CSV/Markdown/JSON-rapporter
- Genererer trade-strategier (entry/breakout/stop/targets/risk/hold)
- Genererer market regime-rapport (SPY/QQQ/SOXX/BTC/VIX + sektorstyrke)
- Genererer kort shareable `trading_brief_YYYYMMDD_HHMM.md` i `reports/shareable/`
- Legger til Top Focus Today, action labels, best next action, confidence score (1-10), catalyst quality, liquidity guardrails, Nordnet alert-nivåer, trigger-regler og portefølje-overlap i trading brief
- Støtter USA-, Nordic- og global-kjøringer via GitHub Actions
- Støtter Nordic-universvalg (`large_caps`, `momentum`, land, `small_caps`, `all`) via workflow input
- Logger market-open anbefalinger i `data/recommendation_log.csv` og skriver snapshots/resultatrapporter i `reports/performance/`
- Støtter midday re-scan med oppdatert fokusliste i `reports/intraday/`
- Leser `config/portfolio.yaml` for eksponeringsvarsler
- Lar deg loggføre trades i `data/trade_journal.csv` og oppsummere læring med `performance_review.py`
- Legger til egen `Do-not-chase warning` for tickere som er kraftig opp, men langt under intradag-high
- Inkluderer debug-felter i CSV som `day_change_source`, `spread_pct` og `spread_bps`
- Viser hvilke Yahoo-lister hver aksje dukket opp i (f.eks. `[Top Gainers, Most Active]`)

> Merk: `yfinance` er uoffisielt og kan bli rate-limitet eller endre seg hvis Yahoo endrer API/dataformat.
> Premarket/after-hours-data kan være ufullstendig avhengig av Yahoo-tilgjengelighet.

## Kom i gang

```bash
pip install -r requirements.txt

# Watchlist-modus (personlige tickere)
python screener.py --input watchlist.csv --outdir .

# Anbefalt daglig modus (bred dekning)
python screener.py --source yahoo-expanded --limit 25 --outdir reports

# Nordic watchlist-modus
python screener.py --market nordic --run-type open --source watchlist --nordic-universe large_caps --outdir reports/nordic

# Konservativ modus (kun momentum-signaler)
python screener.py --source yahoo-momentum --limit 25 --outdir reports

# Enkeltscreener (f.eks. Top Gainers)
python screener.py --source yahoo-gainers --limit 25 --outdir reports

# Strategy fra eksisterende momentum-CSV
python strategy_engine.py --input reports/momentum_report_YYYYMMDD_HHMM.csv --outdir reports

# Market regime
python market_regime.py --outdir reports

# Oppdater anbefalingsresultater etter close
python recommendation_tracker.py --mode same-day

# Oppdater anbefalingsresultater etter ca. 5 handelsdager
python recommendation_tracker.py --mode 1w

# Midday placeholder / polling-run
python intraday_monitor.py --input reports/usa/momentum_report_YYYYMMDD_HHMM.csv --market usa

# Oppsummer trade journal
python performance_review.py
```

Eksempel på output-filer:

- `momentum_report_YYYYMMDD_HHMM.csv`
- `momentum_report_YYYYMMDD_HHMM.md`
- `momentum_report_YYYYMMDD_HHMM.json`
- `strategy_report_YYYYMMDD_HHMM.{csv,md,json}`
- `market_regime_YYYYMMDD_HHMM.{md,json}`
- `shareable/trading_brief_YYYYMMDD_HHMM.md`
- `performance/recommendations_YYYYMMDD_<market>_open.{md,json}`
- `performance/recommendation_results_YYYYMMDD.{md,json}`
- `performance/performance_summary.{md,json}`
- `intraday/intraday_rescan_YYYYMMDD_HHMM.{md,json}`
- `config/portfolio.yaml`
- `data/trade_journal.csv`
- `data/recommendation_log.csv`

A sample report with the new fields is available in `samples/sample_momentum_report.md`.

## CLI-argumenter

| Argument | Standard | Beskrivelse |
|---|---|---|
| `--input` | `watchlist.csv` | CSV med tickere (brukes med `--source watchlist`) |
| `--outdir` | `.` | Mappe for output-filer |
| `--source` | `watchlist` | Datakilde (se tabellen nedenfor) |
| `--limit` | `25` | Maks antall tickere å hente per Yahoo screener |
| `--min-price` | `2.0` | Minimum aksjekurs (Yahoo-modus) |
| `--min-market-cap` | `500000000` | Minimum markedsverdi (Yahoo-modus) |
| `--min-volume` | `1000000` | Minimum volum (Yahoo-modus) |
| `--nordic-universe` | `large_caps` | Universe for Nordic/global watchlist-modus (`large_caps`, `momentum`, land, `small_caps`, `all`) |
| `--data-sources-config` | `config/data_sources.yaml` | Konfigurasjon av USA data-provider (`yahoo`/`alpaca`) |

### Tilgjengelige kilder (`--source`)

#### Grupperte kilder (henter fra flere screeners og deduplicerer)

| Verdi | Beskrivelse |
|---|---|
| `watchlist` | Bruker `--input` CSV (standard) — watchlist-modus for personlige tickere |
| `yahoo-expanded` | **Anbefalt daglig modus** — Yahoo-screeners kombinert og deduplisert (ustabile valgfrie kilder kan være deaktivert) |
| `yahoo-momentum` | **Konservativ modus** — momentum-orienterte Yahoo-screeners (ustabile valgfrie kilder kan være deaktivert) |
| `yahoo-all` | Alias for `yahoo-momentum` (beholdt for bakoverkompatibilitet) |

#### Individuelle Yahoo-screeners

| Verdi | Beskrivelse |
|---|---|
| `yahoo-gainers` | Top Gainers fra Yahoo Finance |
| `yahoo-most-active` | Most Active fra Yahoo Finance |
| `yahoo-trending` | Trending Now fra Yahoo Finance |
| `yahoo-unusual-volume` | Unusual Volume fra Yahoo Finance |
| `yahoo-high-beta` | High Beta Stocks fra Yahoo Finance |
| `yahoo-losers` | Top Losers fra Yahoo Finance |
| `yahoo-oversold` | Oversold Stocks fra Yahoo Finance |
| `yahoo-overbought` | Overbought Stocks fra Yahoo Finance |
| `yahoo-52-week-gainers` | 52-Week Gainers fra Yahoo Finance |
| `yahoo-all-time-high` | All-Time High Stocks fra Yahoo Finance |

Grupperte modus viser hvilke lister hver aksje dukket opp i, f.eks.:

```
- NVDA [Top Gainers, Most Active]: score 80. green, volume > 2x ...
```

> **Merk:** Noen Yahoo predefined screener-IDer kan returnere HTTP 404. Disse hoppes over slik at resten av kjøringen fortsetter normalt.
> Helsesjekk skrives til `reports/data_quality/screener_health_YYYYMMDD_HHMM.{md,json}`.

## Watchlist-format

`watchlist.csv` må minst ha kolonnen `ticker`.

Støttede kolonner:

- `ticker` (påkrevd)
- `category` (valgfri)
- `news` (valgfri bool: true/false)
- `sector_strength` (valgfri bool: true/false)

Eksempel:

```csv
ticker,category,news,sector_strength
FTNT,cyber,false,true
DDOG,cloud,true,true
```

Nordic-univers ligger i egne filer:

- `watchlists/nordic_large_caps.csv`
- `watchlists/nordic_momentum.csv`
- `watchlists/norway.csv`
- `watchlists/sweden.csv`
- `watchlists/denmark.csv`
- `watchlists/finland.csv`
- `watchlists/nordic_small_caps.csv`

Bruk `--nordic-universe all` for å kombinere alle og deduplisere tickerne.

## Data providers (USA)

Discovery-listene for USA hentes fortsatt fra Yahoo-screeners. Pris/volum-bars kan styres via `config/data_sources.yaml`:

```yaml
usa_data_provider: yahoo
# allowed: yahoo, alpaca
```

- `yahoo`: bruker Yahoo/yfinance som før
- `alpaca`: bruker Alpaca for USA latest/intraday/historical bars når Alpaca-credentials er satt
- Manglende Alpaca-credentials gir automatisk fallback til Yahoo
- Alpaca brukes kun til markedsdata (ingen ordrelegging)

## Begrensninger (Yahoo/yfinance)

- Yahoo/yfinance er uoffisielt og kan endre seg eller bli rate-limitet.
- Enkelte predefined screener-IDer kan bli utilgjengelige over tid.
- Premarket/after-hours-data kan være ufullstendig.

## Legg til flere nordiske tickere

1. Legg ticker i en av filene i `watchlists/` med kolonnene `ticker,company,country,exchange,theme,liquidity_tier`.
2. Kjør med ønsket `--nordic-universe`.
3. Bruk `all` for kombinert og deduplisert univers.

## Portfolio og journal

- `config/portfolio.yaml` brukes til å flagge sektor-/tema-overlapp før nye trades tas.
- `data/trade_journal.csv` er en enkel journal for dato, setup, entry/exit, størrelse, stop, target, resultat, plan-follow, entry-type, stop-respect, action-label match og lesson learned.
- `data/recommendation_log.csv` lagrer market-open anbefalinger, samme-dag-close og 1-ukes-resultater.
- `python performance_review.py` oppsummerer både trade journal og anbefalingsstatistikk (win rate, snittavkastning, beste/verste markeder, setup-typer og action labels).

## Recommendation Performance Tracking

- Screeneren logger hva som ble anbefalt ved Nordic market open og USA market open.
- Same-day resultater sjekkes etter market close via `recommendation_tracker.py --mode same-day`.
- One-week resultater sjekkes etter omtrent 5 handelsdager via `recommendation_tracker.py --mode 1w`.
- Disse resultatene gjør det mulig å måle om anbefalingene faktisk er nyttige over tid.

Eksempel:

```text
Date: 2026-05-08
Market: USA
Recommended at open:
- AMD — BUY SETUP
- MU — WATCH
- DDOG — WATCH

Same-day result:
- AMD: +2.1%
- MU: +1.4%
- DDOG: -0.6%

One-week result:
- AMD: pending
- MU: pending
- DDOG: pending
```

## Data connection validation

Use the manual workflow **Validate Data Connections** before market hours when you want a quick end-to-end health check of data providers and report pipelines.

- Workflow: `.github/workflows/validate-data-connections.yml`
- Script: `python scripts/validate_data_connections.py --outdir reports/data_quality`
- Optional script flags:
  - `--usa-data-provider yahoo|alpaca`
  - `--nordic-universe large_caps|momentum|norway|sweden|denmark|finland|small_caps|all`

Alpaca secrets support both conventions:

- Preferred: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`
- Backward compatible: `ALPACA_KEY`, `ALPACA_SECRET`

Alpaca is used for **data only** in validation (no order/trading endpoints are called). Yahoo remains the discovery source, and Nordic universes are watchlist-based CSV files under `watchlists/`.

Validation artifacts are written to `reports/data_quality/` as Markdown and JSON:

- `PASS`: all critical checks succeeded
- `WARN`: non-critical issues (for example missing Alpaca credentials with Yahoo fallback)
- `FAIL`: critical connection or schema incompatibility that should be fixed before relying on screener output
