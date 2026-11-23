"""
Daily jobs post for the Telegram channel (user, 2026-11-18): a reel and 1-2 links a day gave nobody a reason to join, so
the channel gets one short message of N fresh jobs every day, whatever the reel is about (a value episode has none).

    python tools/telegram-publisher/daily_jobs.py --folder content/<date> [--dry-run]

Jobs: the reel's own jobs first (brief.json), then one per niche from [daily_jobs] niches, starting at a different
niche each day, until `count`. They come from the job feed through content-data-preparation's own filters (fresh,
fresher-level, live apply link, distinct companies). Each job carries its niche as a hashtag, so a reader can tap
#data or #remote to see only those. Needs TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL and JOB_FEED_API_KEY.
Writes telegram_jobs_message_id into the folder's posted.json and skips when it is already there; the jobs themselves go
to the folder's daily-jobs.json (the LinkedIn post shows the first two).
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
import re
import sys
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
IST = timezone(timedelta(hours=5, minutes=30))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tg_pub = load("tg_publish", HERE / "publish.py")
prep = load("prep_content", ROOT / "tools" / "content-data-preparation" / "prepare_content.py")


def reel_jobs(folder: Path | None) -> list[dict]:
    if not folder:
        return []
    brief = tg_pub.read_json(folder / "brief.json")
    return [j for j in brief.get("jobs", []) if j.get("apply_url") and j.get("title")]


def pick_jobs(count: int, niches: list[dict], first: list[dict], today: date, log=print, exclude: str = "") -> list[dict]:
    """The reel's jobs, then one per niche (rotating start) until `count`; one job per company. Titles matching `exclude`
    (gig / freelance / volunteer work: 2026-11-18's first dry run picked "Mystery Evaluators (Gig ...)") are left out."""
    skip = re.compile(exclude, re.I) if exclude else None
    cfg = prep.load_config(ROOT / "tools" / "content-data-preparation" / "config.toml")
    key = os.environ.get(cfg["api"]["key_env"], "").strip()
    if not key:
        sys.exit(f"set {cfg['api']['key_env']}")
    chosen = [{**j, "tag": niches[0]["tag"]} for j in first[:count]]
    seen = {j["company"].lower() for j in chosen}
    start = today.toordinal() % len(niches)
    order = niches[start:] + niches[:start]
    for round_ in range(2):  # a second round when a niche had nothing today
        for n in order:
            if len(chosen) >= count:
                return chosen
            filters = cfg["niches"].get(n["niche"], {}).get("filters")
            if filters is None:
                log(f"  unknown niche {n['niche']!r}, skipped")
                continue
            try:
                _, usable = prep.choose_jobs(cfg, key, filters, 0, [], today, lambda *_: None)
            except RuntimeError as e:
                log(f"  {n['niche']}: {e}")
                continue
            for j in usable:
                j = {**j, "company": re.sub(r"^home\s*\|\s*", "", j["company"], flags=re.I)}  # "Home | Jobbasket"
                if j["company"].lower() in seen or (skip and skip.search(j["title"])):
                    continue
                if cfg["rules"].get("check_links", True) and not prep.link_is_live(j["apply_url"]):
                    continue
                chosen.append({**j, "tag": n["tag"]})
                seen.add(j["company"].lower())
                log(f"  {n['niche']}: {j['title']} at {j['company']}")
                break
    return chosen


def message(jobs: list[dict], today: date, footer: str) -> str:
    e = html.escape
    lines = [f"🔥 <b>Aaj ki {len(jobs)} fresher jobs</b> · {today:%d %b}", ""]
    for i, j in enumerate(jobs, 1):
        where = j.get("where") or ""
        posted = f" · {j['posted']}" if j.get("posted") else ""
        title = j["title"] if len(j["title"]) <= 60 else j["title"][:59].rsplit(" ", 1)[0].rstrip(" -,(/|") + "…"
        lines.append(f"{i}. <b>{e(title)}</b> · {e(j['company'])}")
        lines.append(f"📍 {e(where)}{posted} · <a href=\"{e(j['apply_url'], quote=True)}\">Apply</a> #{e(j['tag'])}")
        lines.append("")
    if footer:
        lines.append(e(footer))
    return "\n".join(lines).strip()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", type=Path, help="the day's reel folder (its jobs go first; posted.json gets the record)")
    p.add_argument("--dry-run", action="store_true", help="pick the jobs and show the message; post nothing")
    p.add_argument("--config", type=Path, default=HERE / "config.toml")
    a = p.parse_args()
    cfg = tomllib.loads(a.config.read_text(encoding="utf-8"))
    tg, dj = cfg["telegram"], cfg["daily_jobs"]
    tg_pub.load_env_files([(a.config.resolve().parent / f).resolve() for f in tg.get("env_files", [])])
    folder = a.folder.resolve() if a.folder else None
    record_path = folder / "posted.json" if folder else None
    record = tg_pub.read_json(record_path) if record_path else {}
    if record.get("telegram_jobs_message_id"):
        print(f"daily jobs already posted: {record.get('telegram_jobs_url') or record['telegram_jobs_message_id']}")
        return
    today = datetime.now(IST).date()
    print(f"picking {dj['count']} jobs ...")
    jobs = pick_jobs(dj["count"], dj["niches"], reel_jobs(folder), today, exclude=dj.get("exclude_titles", ""))
    if not jobs:
        sys.exit("no jobs found today: nothing posted")
    text = message(jobs, today, dj.get("footer", ""))
    if a.dry_run:
        print("---\n" + text + "\n---\n(dry run: nothing posted)")
        return
    token, channel = os.environ.get(tg["token_env"], "").strip(), os.environ.get(tg["channel_env"], "").strip()
    if not token or not channel:
        sys.exit(f"set {tg['token_env']} and {tg['channel_env']}")
    channel = channel if channel.startswith(("@", "-")) else "@" + channel
    msg = tg_pub.Bot(token).call("sendMessage", {"chat_id": channel, "text": text, "parse_mode": "HTML",
                                                 "link_preview_options": {"is_disabled": True}})
    link = f"https://telegram.me/{channel[1:]}/{msg['message_id']}" if channel.startswith("@") else ""
    print(f"daily jobs posted ({len(jobs)}): {link or msg['message_id']}")
    if record_path:
        record = tg_pub.read_json(record_path)
        record.update({"telegram_jobs_message_id": msg["message_id"], "telegram_jobs_url": link, "telegram_jobs_count": len(jobs)})
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        # the same jobs for the LinkedIn post (linkedin-publisher takes the first 2), so both say the same thing
        keep = ("title", "company", "where", "posted", "apply_url", "tag")
        (folder / "daily-jobs.json").write_text(json.dumps([{k: j.get(k) for k in keep} for j in jobs], indent=1,
                                                           ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
