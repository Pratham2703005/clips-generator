"""
content-data-preparation: decides what today's reel is about and gathers the real data for it. See README.md.

    python tools/content-data-preparation/prepare_content.py [--date YYYY-MM-DD] [--config FILE]
           [--track job_drop|product_pitch] [--niche ID] [--format ID] [--platform ID] [--reroll N] [--dry-run]

Writes <output.dir>/<date>/brief.json and records the day in the history file (not with --dry-run).
The date seeds the random choices. A second run for a date that already has a reel in the history picks something else
(its picks count as used, and the seed moves on), so two reels of one day never share a format, cast, hook or jobs.
--replace instead overwrites that day's entry and repeats its picks (for remaking the same reel); --reroll N moves the
seed by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"
SECTION = "content"  # this tool's table in the master config and the mode files

def load_config(path: Path, section: str = SECTION) -> dict:
    """This tool's own config (complete: the tool runs and is configured on its own), then the master config's
    [<section>] (config.toml at the repo root, or $VG_MASTER) and the run mode's [<section>] ($VG_MODE_FILE, set by
    run.py) on top. A path in an overlay is relative to that overlay file."""
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[2]
    for f in (os.environ.get("VG_MASTER") or root / "config.toml", os.environ.get("VG_MODE_FILE"), os.environ.get("VG_SET_FILE")):
        if f and Path(f).is_file():
            over = tomllib.loads(Path(f).read_text(encoding="utf-8")).get(section)
            if over:
                _merge(cfg, over, Path(f).resolve().parent, section)
    return cfg


def _merge(dst: dict, src: dict, src_dir: Path, where: str) -> None:
    for k, v in src.items():
        if k not in dst:
            print(f"config: {where}.{k} is not in the tool's own config.toml; add it there too", file=sys.stderr)
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v, src_dir, f"{where}.{k}")
            continue
        is_path = where.endswith(".paths") or k.endswith(("_dir", "_file")) or k in ("env_files", "file")
        if is_path and isinstance(v, str) and v and not Path(v).is_absolute():
            v = str((src_dir / v).resolve())
        elif is_path and isinstance(v, list):
            v = [str((src_dir / x).resolve()) if isinstance(x, str) and not Path(x).is_absolute() else x for x in v]
        dst[k] = v

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


# ---- config, env, history ----

def load_env_files(files: list[Path]) -> None:
    """KEY=VALUE lines; a variable already set in the environment wins."""
    for f in files:
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_history(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8")).get("days", []) if path.exists() else []


def recent(history: list[dict], today: date, days: int) -> list[dict]:
    """History entries from the `days` days up to and including `today`: an earlier reel of the same day counts too, so a
    second run picks something else (--replace removes today's entry first when the same reel is wanted again)."""
    since = today - timedelta(days=days)
    return [h for h in history if since <= date.fromisoformat(h["date"]) <= today]


def pick(rng: random.Random, options: list[str], used_recently: set[str], last_used: dict[str, str], weights: dict[str, float] | None = None) -> str:
    """Random pick among options not used recently; if all were, the one used longest ago."""
    fresh = [o for o in options if o not in used_recently]
    if not fresh:
        return min(options, key=lambda o: last_used.get(o, ""))
    return rng.choices(fresh, weights=[(weights or {}).get(o, 1.0) for o in fresh])[0]


def pick_ideas(base: Path, cfg: dict, track: str, rng: random.Random, history: list[dict], today: date, rules: dict, log,
               cast: list[str] | None = None) -> dict:
    """One premise, hook formula and dynamic from banks.toml, each kept away from recent reels by its cooldown
    (user, 2026-10-30: three reels in a row opened on "200 applications" and a roast)."""
    f = base / cfg.get("banks", {}).get("file", "banks.toml")
    if not f.exists():
        return {}
    banks = tomllib.loads(f.read_text(encoding="utf-8"))
    out = {}
    for key, bank, cooldown in (("premise", "premises", "premise_cooldown_days"), ("hook", "hooks", "hook_cooldown_days"),
                                ("dynamic", "dynamics", "dynamic_cooldown_days")):
        # `knows` / `learns`: an idea that only works with a certain character on that side (e.g. "insider-leak" needs
        # HR Malhotra as the one who knows; "reverse-mentor" needs Verma as the learner)
        knows, learns = (cast or [None, None])[0], (cast or [None, None])[-1]
        items = {x["id"]: x for x in banks.get(bank, []) if track in x.get("fits", [track])
                 and (not cast or knows in x.get("knows", [knows])) and (not cast or learns in x.get("learns", [learns]))}
        if not items:
            continue
        chosen = pick(rng, list(items), {h.get(key) for h in recent(history, today, rules.get(cooldown, 7))}, last_used_map(history, key))
        out[key] = {k: v for k, v in items[chosen].items() if k not in ("fits", "knows", "learns")}
    if out:
        log(f"  premise={out.get('premise', {}).get('id')} hook={out.get('hook', {}).get('id')} dynamic={out.get('dynamic', {}).get('id')}")
    return out


def last_used_map(history: list[dict], key: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in history:
        if h.get(key):
            out[str(h[key])] = max(out.get(str(h[key]), ""), h["date"])
    return out


# ---- API ----

def api_get(cfg: dict, path: str, params: dict | None = None, key: str | None = None) -> dict:
    url = cfg["api"]["base_url"].rstrip("/") + path + ("?" + urllib.parse.urlencode(params) if params else "")
    # Cloudflare answers urllib's default "Python-urllib/3.x" User-Agent with error 1010 (browser signature banned).
    headers = {"Accept": "application/json", "User-Agent": "video-generator/1.0 (content-data-preparation)"}
    if key:
        headers["x-api-key"] = key
    limit = cfg["api"].get("timeout_seconds", 60)
    for attempt in range(2):  # some filtered queries are slow on a cold cache; one retry
        try:
            req = urllib.request.Request(url, headers=headers)
            body = with_deadline(lambda: json.loads(urllib.request.urlopen(req, timeout=limit).read().decode("utf-8")), limit)
            if not body.get("success", True):
                raise RuntimeError(f"{path}: {body}")
            return body.get("data", body)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{path}: HTTP {e.code} {e.read()[:200]!r}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 1:
                raise RuntimeError(f"{path}: {e}") from e
            time.sleep(3)
    raise AssertionError("unreachable")


def with_deadline(fn, seconds: float):
    """urlopen's timeout is per socket read, so a slow trickle can run far longer (an /jobs call hung 18+ min on
    2026-10-29 while curl got the same URL in 2 s). This caps the whole call; one that runs over is left in a daemon thread."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # handed to the caller below
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError(f"no reply within {seconds} s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def link_is_live(url: str) -> bool:
    """200 and not a closed-job redirect. A site that blocks scripts or is slow counts as not live: it is skipped."""
    def get():
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200 and "error=true" not in r.geturl()
    for _ in range(3):  # this network resets ~40% of TLS connections for a moment (2026-10-30): retry those
        try:
            return with_deadline(get, 15)
        except Exception as e:
            if not re.search(r"10054|ConnectionReset|RemoteDisconnected|EOF occurred", repr(e)):
                return False
            time.sleep(1)
    return False


# ---- jobs ----

def posted_label(posted: str, today: date) -> str:
    try:
        d = (today - date.fromisoformat(posted[:10])).days
    except ValueError:
        return ""
    return "today" if d <= 0 else "yesterday" if d == 1 else f"{d} days ago"


LEGAL_SUFFIX = re.compile(r"[,\s]+(inc\.?|incorporated|llc|llp|ltd\.?|limited|plc|gmbh|ag|s\.?a\.?|b\.?v\.?|pvt\.? ltd\.?|private limited|private|pvt\.?|sdn\.? bhd\.?|sp\.? z ?o\.? ?o\.?|corp\.?|corporation|n\.\s?a\.)$", re.I)


def clean_company(name: str) -> str:
    """Board names as a person reads them, the same rules as PoolBox's companyDisplayName (2026-11-01: a job card read
    "Xapads - Join Our Global AdTech Innovation Team")."""
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^\(\d+\)\s*", "", name)  # "(0122) Sanofi"
    # a recruiting tagline after a dash: "Xapads - Join Our Global AdTech Innovation Team", "Acme | We're Hiring"
    name = re.sub(r"\s*[-–|:]\s*(join|we.?re hiring|careers?|jobs|work with|be part|open positions)\b.*$", "", name, flags=re.I) or name
    for _ in range(3):  # brackets and legal suffixes peel off in turns: "Flextronics Technologies (India) Private"
        name = LEGAL_SUFFIX.sub("", name).strip()
        name = re.sub(r"\s*\([^()]*\)\s*$", "", name).strip() or name  # "(Abbottcareers2)", "(India)"
    name = re.sub(r"\s+[1-3]$", "", name)  # "Appen 2"
    if " " not in name:
        name = re.sub(r"^([A-Za-z][A-Za-z&.-]{3,}?)[1-3]$", r"\1", name)  # "LinkedIn3", "Inetum2" (not KnowBe4)
    letters = re.sub(r"[^A-Za-z]", "", name)
    if " " in name and len(letters) > 5 and (letters.isupper() or letters.islower()):  # "ROUNDHOUSE COMMUNITIES"
        name = " ".join(w if (len(w) <= 3 and w.isupper()) else w.capitalize() for w in name.split(" "))
    elif name.islower():
        name = name[:1].upper() + name[1:]
    return name.strip(" -,&")


# The audience is freshers (user, 2026-10-30): a senior role is no use to them, in any niche (all three workflow reels of
# 2026-10-31/11-01 featured one: "Senior Medical Writer", "Senior Technical Writer", "Senior GCP Data Engineer").
# "manager" stays: the product-managers niche and entry roles ("Account Manager", "Store Manager") need it.
SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|snr|lead(?!\s*gen)|principal|staff|director|head of|vice president|vp|chief|architect|iii|iv)\b", re.I)


def too_senior(job: dict) -> bool:
    return bool(SENIOR_TITLE.search(job.get("title", ""))) or str(job.get("experience") or "").lower().replace(" ", "") in ("senior", "staff-plus", "staff+", "staffplus")


def clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title.replace("�", "-")).strip()
    title = re.sub(r"\s*\((m/f/d|f/m/d|m/w/d|w/m/d|m/f|all genders|gn)\)", "", title, flags=re.I)
    title = re.sub(r"\s*[-|]\s*(remote|R\d{5,})$", "", title, flags=re.I)
    return title.strip(" -|")


def normalize(job: dict, today: date) -> dict:
    """The fields a reel needs, named for the script writer."""
    where = "Remote" if job.get("isRemote") else ", ".join(x for x in [job.get("city"), job.get("country")] if x) or job.get("location") or ""
    if job.get("isRemote") and job.get("country"):
        where = f"Remote ({job['country']})"
    return {
        "id": job["id"],
        "title": clean_title(job["title"]),
        "company": clean_company(job["company"]),
        "where": where,
        "work_mode": job.get("workMode"),
        "experience": job.get("experienceLevel"),
        "category": job.get("category"),
        "subcategory": job.get("subcategory"),
        "posted_at": job.get("postedAt"),
        "posted": posted_label(job.get("postedAt") or "", today),
        "apply_url": job["applyUrl"],
        "source": job.get("source"),
    }


def choose_jobs(cfg: dict, key: str, filters: dict, need: int, history: list[dict], today: date, log) -> tuple[list[dict], list[dict]]:
    """Returns (chosen jobs, all usable candidates). Chosen: fresh, distinct companies, not featured recently, live links."""
    rules = cfg["rules"]
    params = {**filters, "postedWithinDays": rules["posted_within_days"], "limit": rules["candidates"]}
    raw = api_get(cfg, "/jobs", params, key).get("jobs", [])
    bad = re.compile(rules.get("exclude_companies", "$^"), re.I)
    used_jobs = {j for h in recent(history, today, rules["job_cooldown_days"]) for j in h.get("job_ids", [])}
    used_cos = {c.lower() for h in recent(history, today, rules["company_cooldown_days"]) for c in h.get("companies", [])}
    usable = [normalize(j, today) for j in raw if j.get("applyUrl") and j.get("title") and not bad.search(j.get("company", ""))]
    blocked = set(rules.get("blocked_job_ids", []))  # jobs whose own page showed they can't be featured (volunteer, closed)
    usable = [j for j in usable if j["id"] not in used_jobs and j["id"] not in blocked and j["company"].lower() not in used_cos and j["where"]]
    if rules.get("fresher_audience", True):
        senior = sum(1 for j in usable if too_senior(j))
        usable = [j for j in usable if not too_senior(j)]
        if senior:
            log(f"  {senior} senior/lead/manager roles left out (fresher audience)")
    log(f"  {len(raw)} fetched, {len(usable)} usable after filters and history")
    chosen, seen, dead = [], set(), set()
    for j in usable:
        if len(chosen) >= need:
            break
        # one failed link usually means the whole career site blocks scripts (EWOR: 12 jobs, 12 failures), so skip the rest
        if j["company"].lower() in seen or j["company"].lower() in dead:
            continue
        if rules.get("check_links", True) and not link_is_live(j["apply_url"]):
            log(f"  closed or unreachable, skipped: {j['company']} - {j['title']}")
            dead.add(j["company"].lower())
            continue
        chosen.append(j)
        seen.add(j["company"].lower())
    return chosen, usable


# ---- main ----

def main() -> None:
    p = argparse.ArgumentParser(description="Pick today's reel and gather its data.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--date", help="YYYY-MM-DD (default: today, UTC)")
    p.add_argument("--track", choices=["job_drop", "product_pitch"])
    p.add_argument("--niche")
    p.add_argument("--format")
    p.add_argument("--platform")
    p.add_argument("--cast", help="LEFT,RIGHT character ids (left knows, right learns), instead of picking a cast")
    p.add_argument("--jobs", type=int, help="how many jobs to pick, instead of the format's count (a series episode's slots)")
    p.add_argument("--reroll", type=int, default=0, help="pick again for the same date (any number)")
    p.add_argument("--replace", action="store_true", help="overwrite this date's history entry instead of adding a second reel")
    p.add_argument("--dry-run", action="store_true", help="print the brief, write nothing")
    args = p.parse_args()

    cfg = load_config(args.config)
    base = args.config.resolve().parent
    load_env_files([(base / f).resolve() for f in cfg["api"].get("env_files", [])])
    key = os.environ.get(cfg["api"]["key_env"], "").split(",")[0].strip()
    if not key:
        sys.exit(f"API key missing: set {cfg['api']['key_env']} in the environment or in one of {cfg['api'].get('env_files')}")

    today = date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).date()
    out_dir = (base / cfg["output"]["dir"]).resolve()
    history_path = (base / cfg["output"]["history"]).resolve()
    history = load_history(history_path)
    same_day = [h for h in history if h["date"] == today.isoformat()]
    if args.replace:
        history = [h for h in history if h["date"] != today.isoformat()]
        same_day = []
    elif same_day:
        # 2026-10-31 got two reels (a manual run and the cron) with the same track, format, cast and hook: the earlier
        # run's entry now stays, counts as used, and moves the seed
        log_ = f"{len(same_day)} reel(s) already made for {today.isoformat()}: picking different ones (--replace to redo that reel)"
        print(log_, file=sys.stderr)
    rng = random.Random(f"{today.isoformat()}#{args.reroll + len(same_day)}")
    rules, formats, niches, platforms, chars = cfg["rules"], cfg["formats"], cfg["niches"], cfg["platforms"], cfg["characters"]
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731

    for name, value, table in [("niche", args.niche, niches), ("format", args.format, formats), ("platform", args.platform, platforms)]:
        if value and value not in table:
            sys.exit(f"unknown {name} '{value}'. Options: {', '.join(table)}")

    # 1. track, format, platform, cast
    track = args.track or (formats[args.format]["track"] if args.format else rng.choices(list(cfg["tracks"]), weights=list(cfg["tracks"].values()))[0])
    track_formats = [f for f, v in formats.items() if v["track"] == track]
    fmt = args.format or pick(rng, track_formats, {h.get("format") for h in recent(history, today, rules["format_cooldown_days"])}, last_used_map(history, "format"))
    if formats[fmt]["track"] != track:
        sys.exit(f"format '{fmt}' belongs to {formats[fmt]['track']}, not {track}")
    platform = args.platform or pick(rng, list(platforms), {h.get("platform") for h in recent(history, today, rules["platform_cooldown_days"])}, last_used_map(history, "platform"))
    casts = [tuple(c) for c in cfg["casts"][track]]
    cast_ids = {"+".join(c): c for c in casts}
    if args.cast:
        pair = [x.strip().lower() for x in args.cast.split(",")]
        unknown = [x for x in pair if x not in chars]
        if len(pair) != 2 or unknown:
            sys.exit(f"--cast needs two of {', '.join(chars)} (got {args.cast})")
        cast_key = "+".join(pair)
        cast_ids[cast_key] = pair
    else:
        cast_key = pick(rng, list(cast_ids), {h.get("cast") for h in recent(history, today, rules["cast_cooldown_days"])}, last_used_map(history, "cast"))
    log(f"{today}: track={track} format={fmt} platform={platform} cast={cast_key}")

    # 2. niche and jobs: try niches in pick order until one has enough usable jobs
    need = args.jobs or formats[fmt]["jobs"]
    # a job drop also needs the niche to be alive today (min_jobs live), even when the format shows fewer
    want = max(need, rules["min_jobs"]) if track == "job_drop" else max(need, 1)
    used_niches = {h.get("niche") for h in recent(history, today, rules["niche_cooldown_days"])}
    order = [args.niche] if args.niche else sorted(niches, key=lambda n: (n in used_niches, rng.random()))
    jobs, usable, niche = [], [], None
    for n in order:
        log(f"niche {n}: {niches[n]['label']}")
        try:
            jobs, usable = choose_jobs(cfg, key, niches[n]["filters"], want, history, today, log)
        except RuntimeError as e:
            log(f"  API error, trying the next niche: {e}")
            continue
        if len(jobs) >= want:
            niche = n
            break
        log(f"  only {len(jobs)} usable, trying the next niche")
    if niche is None:
        sys.exit("no niche had enough usable jobs today")

    spotlight = None
    if fmt == "company-spotlight":
        counts: dict[str, int] = {}
        for j in usable:
            counts[j["company"]] = counts.get(j["company"], 0) + 1
        # the busiest company whose roles actually open (some career sites block scripts: IBM on 2026-10-29)
        for company, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            if count < 2:
                break
            # one per title: companies post the same role more than once (Mastercard, 2026-10-29)
            roles = list({j["title"].lower(): j for j in reversed([j for j in usable if j["company"] == company])}.values())[::-1][: need + 3]
            if rules.get("check_links", True):
                roles = [j for j in roles if link_is_live(j["apply_url"])]
            if len(roles) >= 2:
                spotlight = {"company": company, "count": count, "note": f"{count} of the {len(usable)} newest {niches[niche]['label'].lower()} fetched"}
                jobs = roles[:need]
                break
            log(f"  spotlight: {company} has {count} roles but only {len(roles)} open, trying the next company")
        if spotlight is None:
            if args.format:
                sys.exit("company-spotlight: no company with 2+ open roles in this niche today; try another format or niche")
            # 2026-11-02: a random pick of company-spotlight + product-managers stopped the whole run; the niche's jobs
            # are already in hand (at least min_jobs), so another job-drop format uses them instead
            others = [f for f in track_formats if f != "company-spotlight"]
            fmt = pick(rng, others, {h.get("format") for h in recent(history, today, rules["format_cooldown_days"])}, last_used_map(history, "format"))
            need = formats[fmt]["jobs"]
            jobs = jobs[:need]
            log(f"  company-spotlight: no company with 2+ open roles here; format {fmt} instead")

    stats = api_get(cfg, "/stats")
    # live jobs in the niche's category or subcategory (all countries), for "number" lines; None when the niche has neither
    filters = niches[niche]["filters"]
    niche_count = None
    for cat in stats.get("categories", []):
        if filters.get("categories") == cat["name"]:
            niche_count = {"scope": f"{cat['name']}, all countries", "live_jobs": cat["jobs"]}
        for sub in cat.get("subcategories", []):
            if filters.get("subcategories") == sub["name"]:
                niche_count = {"scope": f"{sub['name']}, all countries", "live_jobs": sub["jobs"]}
    brief = {
        "date": today.isoformat(),
        "track": track,
        "format": {"id": fmt, **formats[fmt]},
        "niche": {"id": niche, **{k: v for k, v in niches[niche].items() if k != "filters"}, "filters": niches[niche]["filters"]},
        "platform": {"id": platform, **platforms[platform]},
        "cast": [{"id": cid, "position": pos, **chars[cid]} for cid, pos in zip(cast_ids[cast_key], ["left", "right"])],
        "jobs": jobs[:need] if need else [],
        "spotlight": spotlight,
        "stats": {
            "total_jobs": stats.get("jobs"),
            "companies": stats.get("companies"),
            "sources": stats.get("sources"),
            "posted_last_7_days": stats.get("postedLast7Days"),
            "remote_jobs": stats.get("remote"),
            "niche": niche_count,
            "updated_at": stats.get("updatedAt"),
        },
        "facts": cfg.get("facts", {}).get("product", []),
        "context": cfg.get("facts", {}).get("context", []),
        **pick_ideas(base, cfg, track, rng, history, today, rules, log, cast_ids[cast_key]),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if args.dry_run:
        print(json.dumps(brief, indent=2, ensure_ascii=False))
        return
    day_dir = out_dir / today.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "brief.json").write_text(json.dumps(brief, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    history.append({
        "date": today.isoformat(), "track": track, "format": fmt, "niche": niche, "platform": platform, "cast": cast_key,
        "job_ids": [j["id"] for j in brief["jobs"]], "companies": [j["company"] for j in brief["jobs"]],
        **{k: brief[k]["id"] for k in ("premise", "hook", "dynamic") if brief.get(k)},
    })
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(json.dumps({"days": sorted(history, key=lambda h: h["date"])}, indent=2) + "\n", encoding="utf-8")
    log(f"wrote {day_dir / 'brief.json'} ({len(brief['jobs'])} jobs) and updated {history_path.name}")


if __name__ == "__main__":
    main()
