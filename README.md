# Aksjeopplysningne

Lite personlig prosjekt for momentum-screening av aksjer.

## Hva det gjør

- Leser tickere fra `watchlist.csv` eller henter live fra Yahoo Finance screeners
- Henter intradag-data via `yfinance` (pris, volum, høy/lav, pre/after-hours når tilgjengelig)
- Regner ut momentum-score (0-100-ish) basert på enkle regler
- Deler tickere i:
  - `A-list` (score >= 70)
  - `B-list` (score 45-70)
  - `C-list` (score < 45)
- Lager både CSV-rapport og kort Markdown-rapport
- Legger til egen `Do-not-chase warning` for tickere som er kraftig opp, men langt under intradag-high
- Inkluderer debug-felter i CSV som `day_change_source`, `spread_pct` og `spread_bps`

> Merk: `yfinance` er uoffisielt og kan bli rate-limitet eller endre seg hvis Yahoo endrer API/dataformat.
> Premarket/after-hours-data kan være ufullstendig avhengig av Yahoo-tilgjengelighet.

## Kom i gang

```bash
pip install -r requirements.txt

# Watchlist-modus (standard)
python screener.py --input watchlist.csv --outdir .

# Yahoo screener-modus
python screener.py --source yahoo-gainers --limit 25 --outdir reports
python screener.py --source yahoo-all --limit 25 --outdir reports
```

Eksempel på output-filer:

- `momentum_report_YYYYMMDD_HHMM.csv`
- `momentum_report_YYYYMMDD_HHMM.md`

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

| Verdi | Beskrivelse |
|---|---|
| `watchlist` | Bruker `--input` CSV (standard) |
| `yahoo-gainers` | Top Gainers fra Yahoo Finance |
| `yahoo-most-active` | Most Active fra Yahoo Finance |
| `yahoo-trending` | Trending Now fra Yahoo Finance |
| `yahoo-unusual-volume` | Unusual Volume fra Yahoo Finance |
| `yahoo-high-beta` | High Beta Stocks fra Yahoo Finance |
| `yahoo-all` | Alle fem Yahoo-screeners kombinert og deduplisert |

`yahoo-all` viser også hvilke lister hver aksje dukket opp i, f.eks.:

```
- NVDA [Top Gainers, Most Active]: score 80. green, volume > 2x ...
```

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

