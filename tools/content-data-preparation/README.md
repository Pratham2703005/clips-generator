# content-data-preparation

Decides what today's reel is about and gathers the real data for it, so the script writer never invents anything.

Each day it picks:
- a **track**: `job_drop` (jobs for job seekers) or `product_pitch` (promotes one of our platforms), by weight
- a **format** of that track (top-drop, guess-the-company, company-spotlight, number-shock, using-ai-wrong, ...)
- a **niche** (India freshers, remote worldwide, Data & AI, ...) and fetches its newest jobs from the Job Feed API
- a **platform** for the call to action (Apify, n8n, Make, RapidAPI, MCP, ...)
- a **cast**: two characters, left and right

Nothing used recently comes back too soon (cooldowns in the config). Jobs are fresh, from different companies, not featured in the last 30 days, and every apply link is opened before it is used.

## Needs
- Python 3.11+ (standard library only)
- A Job Feed API key in `JOB_FEED_API_KEY`, either in the environment or in the `.env` file at the project root (`KEY=value` lines; the file is git-ignored).

## Use
```
python tools/content-data-preparation/prepare_content.py                 # today
python tools/content-data-preparation/prepare_content.py --dry-run       # print the brief, write nothing
python tools/content-data-preparation/prepare_content.py --date 2026-11-02
python tools/content-data-preparation/prepare_content.py --track product_pitch --platform n8n
python tools/content-data-preparation/prepare_content.py --format guess-the-company --niche india-freshers
python tools/content-data-preparation/prepare_content.py --reroll 1      # different picks for the same date
```
The same date always gives the same picks (the date seeds the random choices). Running a day again replaces that day's brief and history entry.

## Output
- `content/<date>/brief.json`: track, format (with instructions for the script writer), niche, platform (name, how to find it, CTA line), cast (names, personas, catchphrases, sides), the chosen jobs (title, company, where, posted "today"/"yesterday", apply link...), a company spotlight when the format needs one, live stats (total jobs, companies, last 7 days, remote, the niche's live count) and the product and context facts the script may use.
- `content/history.json`: one line per day (track, format, niche, platform, cast, job ids, companies). Commit it, so the daily run remembers what it already posted.

## Settings ([config.toml](config.toml))
| Section | What it holds |
|---|---|
| `api` | API base URL, the env variable holding the key, env files to read, timeout |
| `output` | where briefs and the history go |
| `rules` | jobs per reel, how many to fetch, how fresh, cooldowns (niche, format, platform, cast, job, company), link checks, companies to exclude |
| `tracks` | how often each track runs (weights) |
| `characters.*` | name, nickname, character pack, side, facing, persona, catchphrase |
| `casts` | which characters can appear together, per track |
| `formats.*` | track, how many jobs, instructions for the script writer |
| `niches.*` | label, audience, and the `/jobs` filters (`countries`, `experienceLevels`, `categories`, `subcategories`, `city`, `workModes`, `includeWorldwideRemote`, `company`) |
| `platforms.*` | name, who it is for, how to find it, CTA line |
| `facts.product` | the only claims the script may make about the product (numbers come live from `/stats`) |
| `facts.context` | true background the script may use for the problem and the banter (job boards copying listings, stale listings, chatbots without live search); no numbers, no named sites |

Add a niche, format, platform or character by adding a table; nothing in the code needs to change.

## Notes
- Some filtered queries are slow (a whole category with no country can take over 2 minutes), so the timeout is 180 s with one retry. A full run takes about 2-5 minutes, mostly API time and link checks.
- A career site that blocks scripts counts as "closed"; after one failure the rest of that company's jobs are skipped.
