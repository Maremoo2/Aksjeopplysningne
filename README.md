# Aksjeopplysningne

Lite personlig prosjekt for momentum-screening av aksjer.

## Repository boundary

Aksjeopplysningne is a discretionary momentum-screening and trading-assistant tool. It does not perform live trading, broker execution, autonomous position management, or AI trader shadow/champion model switching. Those belong in the separate AI trader project.

## Hva det gjør

- Leser tickere fra `watchlist.csv` eller henter live fra Yahoo Finance screeners
- Henter intradag-data via `yfinance` (pris, volum, høy/lav, pre/after-hours når tilgjengelig)
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
- Legger til intraday-priority, trigger alerts og portefølje-overlap i trading brief
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

# Konservativ modus (kun momentum-signaler)
python screener.py --source yahoo-momentum --limit 25 --outdir reports

# Enkeltscreener (f.eks. Top Gainers)
python screener.py --source yahoo-gainers --limit 25 --outdir reports

# Strategy fra eksisterende momentum-CSV
python strategy_engine.py --input reports/momentum_report_YYYYMMDD_HHMM.csv --outdir reports

# Market regime
python market_regime.py --outdir reports

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
- `config/portfolio.yaml`
- `data/trade_journal.csv`

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

### Tilgjengelige kilder (`--source`)

#### Grupperte kilder (henter fra flere screeners og deduplicerer)

| Verdi | Beskrivelse |
|---|---|
| `watchlist` | Bruker `--input` CSV (standard) — watchlist-modus for personlige tickere |
| `yahoo-expanded` | **Anbefalt daglig modus** — alle 10 Yahoo-screeners kombinert og deduplisert |
| `yahoo-momentum` | **Konservativ modus** — 5 momentum-orienterte screeners (gainers, most-active, trending, unusual-volume, high-beta) |
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

> **Merk:** Noen Yahoo predefined screener-IDer kan returnere HTTP 404. Disse hoppes over med en advarsel
> slik at resten av kjøringen fortsetter normalt.

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

## Portfolio og journal

- `config/portfolio.yaml` brukes til å flagge sektor-/tema-overlapp før nye trades tas.
- `data/trade_journal.csv` er en enkel journal for dato, setup, entry/exit, størrelse, stop, target, resultat og om planen ble fulgt.
- `python performance_review.py` oppsummerer win rate, gjennomsnittlig gevinst/tap, beste/verste setup og tema, holdetid (når tilgjengelig) og hvor ofte planen ble fulgt.
