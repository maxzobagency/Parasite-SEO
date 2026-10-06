# Parasite SERP Tracker

Checks Google SERPs for 500–1000+ keywords through the DataForSEO API, spots which results are
**parasite pages** (Medium, LinkedIn Pulse, Reddit, Quora, github.io, Google Sites, Forbes Councils,
press-release sites …), and builds an HTML dashboard that shows who is dominating, per platform and per
niche, what changed since the last run, and which unknown domains look like parasite hosts.

Zero dependencies: Python 3.11+ only. Data lives in a local SQLite file.

## Web app (recommended: no terminal, keywords saved, live progress)
```bash
APP_PASSWORD=choose-a-password python -m parasite_tracker.webapp     # then open http://localhost:8000
```
Log in, paste keywords, enter your DataForSEO details and click **Search now**: results appear live
(DataForSEO *Live* mode, ~$0.002 per keyword per 10 results, shown before you confirm). **Nothing is saved on
the server** - keywords and results live in memory for ~1 hour (download the CSV), and your DataForSEO
password is only remembered in your own browser if you tick the box. The queue-based bulk mode
(cheaper, ~$0.0006) is still available from the command line.

**Deploy on Render:** New → Blueprint → pick this repo + branch (it reads `render.yaml`), set `APP_PASSWORD`
when asked. No disk is needed (nothing is stored); the free plan works but sleeps when idle.
The same `Dockerfile` also works on Railway/Fly.io. Always set a strong `APP_PASSWORD`: the app spends your
DataForSEO credit.

## Command line (alternative)
```bash
cp config.example.toml config.toml
cp .env.example .env                    # put your DataForSEO login + API password here
cp keywords.example.csv keywords.csv    # replace with your keywords
python -m parasite_tracker estimate     # expected cost
python -m parasite_tracker run --limit 10   # cheap test on 10 keywords
python -m parasite_tracker run              # full run, then open report.html
```
`keywords.csv` columns: `keyword` (required), `niche`, and optional `location_code`, `language_code`,
`device` to override the defaults per keyword (e.g. different countries in one file).

## Commands
| command | what it does |
|---|---|
| `run` | submit all keywords, wait for results, store, build `report.html` (`--resume` retries a broken run, `--limit N`, `--yes`) |
| `collect` | fetch results of a run you interrupted |
| `report` | rebuild the dashboard from stored data |
| `mark DOMAIN parasite\|ignore` | teach the tool; all stored results are re-classified immediately |
| `export [file.csv]` | full latest SERPs with parasite flags, for Sheets/Excel |
| `estimate` | expected cost of one run |

## How parasite detection works
1. **Rule list** (`parasite_tracker/parasites.toml`) – hosts, sub-domains (`*.github.io`), and path rules
   (`linkedin.com/pulse` yes, `linkedin.com/jobs` no; `forbes.com/sites` yes, `forbes.com/advisor` no).
   Grouped in categories you can switch on/off in `config.toml`.
2. **Your overrides** – `mark` adds domains to the DB, no file editing.
3. **Discovery** – from your own data it flags domains not on the list that behave like parasite hosts
   (many different sub-domains ranking, or one domain ranking across 3+ niches). They appear at the
   bottom of the report; confirm with `mark`.
4. Optional categories: `social_video` (YouTube/Facebook…) and `edu_gov` (abandoned `.edu`/`.gov` pages).

## Which DataForSEO plan?
There is no subscription needed – it is **pay-as-you-go** (minimum top-up about $50, credit doesn't expire
quickly). Use the **Google Organic SERP API, Standard queue** (`priority = 1`): it is the cheapest method;
results come back in roughly 1–5 minutes, which doesn't matter for a scheduled job. Skip "Live" (several
times pricier) unless you need instant answers.
Approximate cost (check [dataforseo.com/pricing](https://dataforseo.com/pricing), rates change):
standard ≈ $0.0006 per 10 results per keyword.

| 1,000 keywords | per run | per month |
|---|---|---|
| top 10, weekly | ~$0.60 | ~$2.60 |
| top 20, weekly (default) | ~$1.20 | ~$5.20 |
| top 20, daily | ~$1.20 | ~$36 |

Tips: weekly is enough for most; run daily only for your money keywords (use a second
`keywords.csv` + `--config`). Parasites mostly sit in the top 10, so depth 10–20 is plenty.

## Scheduling
```cron
0 6 * * 1  cd /path/to/Parasite-SEO && python -m parasite_tracker run --yes >> tracker.log 2>&1
```
(or a GitHub Actions cron / a small VPS; keep `parasite.db` somewhere persistent.)

## Tests
`python -m unittest discover -s tests`
