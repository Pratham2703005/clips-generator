"""The top of the daily report email (user, 2026-11-27: "too boring, I don't see anything, the numbers never make
sense; I want to see what's suspicious, what you changed and why").

Second pass the same day (user: "less text, simple words, only what matters"). The mail is now only:
  subject      the verdict in a few words
  Tonight      one row of chips: did each thing go up
  Fix          problems only, one short line each (where things broke before: voice, posting, tomorrow's
               episode, a flop, stuck followers, a dead profile link, YouTube drop, tokens)
  This week    five number tiles against a goal + one "do this" tile for the biggest gap
               (social-analytics-report / metrics-dashboard skills: exactly five, like with like)
  Charts       PNG (Gmail drops SVG and scripts): views on day 1 per reel, saves+shares per 1k views, followers
  Drop-off     one line of arrows per path
  Changed      changes.toml in "what - why" lines
Every table of the old report moves to the attached file.

Like with like: a reel is judged by its views at ~24 h (snapshot nearest 24 h, 18-32 h after posting), never by
totals of reels of different ages. Raw numbers, rates and interpretation are kept apart.
"""

from __future__ import annotations

import html
import io
import re
import statistics
import subprocess
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# dataviz reference palette (light surface; the email is read on white)
INK, INK2, GRID, AXIS = "#191919", "#666666", "#e7e7e7", "#c2c2c2"
BLUE, BLUE_DARK = "#257de0", "#1757ac"
GOOD, WARN, BAD = "#0aac23", "#fbb315", "#dc3c3d"
ICON = {"bad": "❌", "warn": "⚠️", "good": "✅", "ok": "✅", "info": "ℹ️"}
RANK = {"ok": 0, "good": 0, "info": 0, "warn": 1, "bad": 2}

DEFAULTS = {
    "flop_ratio": 0.5,      # yesterday's reel below this share of the usual 24 h views = flop
    "hit_ratio": 1.5,
    "reels_in_chart": 14,
    "flat_followers_days": 3,
    "changes_days": 3,      # changes.toml entries and commits this many days back
}
TARGETS = {  # [dashboard.targets] in config.toml overrides; picked 2026-11-27 from the Nov numbers (see the README)
    "views_24h": 3000, "saves_shares_per_1k": 15, "followers_per_1k": 2.0, "watch_s": 15, "own_channel_week": 20,
}
ACTIONS = {  # one short "do this", shown for the metric furthest below its goal
    "views_24h": "Stronger line 1 and hook card: copy the opener of the best reel this month.",
    "saves_shares_per_1k": "Put one thing worth saving in every reel (a sample answer, a template).",
    "followers_per_1k": "Bio with one clear promise, pin 3 best reels, end each reel with 'kal ...'.",
    "watch_s": "One idea per reel, cut the first 2 seconds.",
    "own_channel_week": "Say the comment keyword clearly and early; check the DM bot replied.",
}


# ---------- reels at 24 h ----------

def short_title(caption: str | None, n: int = 30) -> str:
    first = (caption or "").splitlines()[0] if caption else ""
    t = first.split(" | ")[0].strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def reels(ig: dict, history: list[dict], now: datetime) -> list[dict]:
    """Instagram reels of the window, oldest first, with views / saves+shares at ~24 h: the live numbers when the reel
    is 18-32 h old now, else the snapshot whose age is nearest 24 h in 18-32 h; None when no snapshot fits."""
    snaps = [(datetime.fromisoformat(s["at"]), (s.get("ig") or {}).get("posts") or {}) for s in history]
    out = []
    for m in ig.get("posts") or []:
        if m.get("media_product_type") not in (None, "REELS"):
            continue
        ts = m["ts"]
        age = (now - ts).total_seconds() / 3600
        v24 = ss24 = None
        if 18 <= age <= 32:
            v24, ss24 = m.get("views"), _ss(m.get("saved"), m.get("shares"))
        else:
            fit = []
            for at, posts in snaps:
                p = posts.get(m["id"])
                h = (at - ts).total_seconds() / 3600
                if p and p.get("views") is not None and 18 <= h <= 32:
                    fit.append((abs(h - 24), p["views"], _ss(p.get("saves"), p.get("shares"))))
            if fit:
                _, v24, ss24 = min(fit, key=lambda f: f[0])
        watch = m.get("ig_reels_avg_watch_time")
        out.append({"id": m["id"], "ts": ts, "age_h": age, "title": short_title(m.get("caption")), "views": m.get("views"),
                    "v24": v24, "ss24": ss24, "watch_s": watch / 1000 if watch else None, "permalink": m.get("permalink", "")})
    return sorted(out, key=lambda r: r["ts"])


def _ss(saves, shares):
    return None if saves is None and shares is None else int(saves or 0) + int(shares or 0)


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def per_1k(part, whole):
    return part / whole * 1000 if part is not None and whole else None


# ---------- the five numbers ----------

def snapshot_near(history: list[dict], when: datetime, slack_h: float = 18) -> dict | None:
    fit = [(abs((datetime.fromisoformat(s["at"]) - when).total_seconds()) / 3600, s) for s in history]
    fit = [f for f in fit if f[0] <= slack_h]
    return min(fit, key=lambda f: f[0])[1] if fit else None


def ig_views_total(s: dict | None) -> int | None:
    posts = ((s or {}).get("ig") or {}).get("posts")
    return sum(int(p["views"]) for p in posts.values() if p.get("views") is not None) if posts else None


def followers_per_1k(history: list[dict], cur: dict, start: datetime, end: datetime) -> float | None:
    """IG followers gained per 1,000 views gained between two snapshots (views summed over the posts both know,
    a post new since then counting from 0)."""
    a = snapshot_near(history, start)
    b = cur if end >= datetime.now(timezone.utc) - timedelta(hours=1) else snapshot_near(history, end)
    if not a or not b or not a.get("ig") or not b.get("ig"):
        return None
    gained_views = sum(int(p["views"]) - int(a["ig"]["posts"].get(pid, {}).get("views") or 0)
                       for pid, p in b["ig"]["posts"].items() if p.get("views") is not None)
    df = (b["ig"].get("followers") or 0) - (a["ig"].get("followers") or 0)
    return per_1k(df, gained_views) if gained_views > 0 else None


def five_numbers(rows: list[dict], history: list[dict], cur: dict, ig: dict, dm: dict, now: datetime,
                 targets: dict) -> list[dict]:
    week, prev = now - timedelta(days=7), now - timedelta(days=14)
    last = [r for r in rows if r["ts"] >= week and r["v24"] is not None]
    before = [r for r in rows if prev <= r["ts"] < week and r["v24"] is not None]

    def ss_rate(rs):
        v = sum(r["v24"] for r in rs if r["ss24"] is not None)
        s = sum(r["ss24"] for r in rs if r["ss24"] is not None)
        return per_1k(s, v)
    acct = ((ig.get("account") or {}).get("values")) or {}
    week_dm = (dm or {}).get("week") or {}
    own = None
    if week_dm or acct.get("profile_links_taps") is not None:
        own = int(week_dm.get("dms", 0)) + int(acct.get("profile_links_taps") or 0)
    out = [
        {"key": "views_24h", "name": "Views on day 1", "how": "usual reel this week",
         "now": median(r["v24"] for r in last), "prev": median(r["v24"] for r in before), "fmt": "{:,.0f}"},
        {"key": "saves_shares_per_1k", "name": "Saves + shares", "how": "per 1,000 views",
         "now": ss_rate(last), "prev": ss_rate(before), "fmt": "{:.1f}"},
        {"key": "followers_per_1k", "name": "New followers", "how": "per 1,000 views",
         "now": followers_per_1k(history, cur, week, now), "prev": followers_per_1k(history, cur, prev, week), "fmt": "{:.2f}"},
        {"key": "watch_s", "name": "Watch time", "how": "usual reel this week",
         "now": median(r["watch_s"] for r in last if r["watch_s"]), "prev": median(r["watch_s"] for r in before if r["watch_s"]),
         "fmt": "{:.1f} s"},
        {"key": "own_channel_week", "name": "Came to us", "how": "DMs + link taps, 7 days",
         "now": own, "prev": None, "fmt": "{:,.0f}"},
    ]
    for m in out:
        t = targets.get(m["key"])
        m["target"] = t
        m["status"] = ("info" if m["now"] is None or t is None else
                       "good" if m["now"] >= t else "warn" if m["now"] >= t / 2 else "bad")
        m["action"] = ACTIONS[m["key"]] if m["status"] in ("warn", "bad") else ""
    return out


def fmt(m: dict, v) -> str:
    return "–" if v is None else m["fmt"].format(v)


def trend(m: dict) -> str:
    if m["now"] is None or m["prev"] is None or not m["prev"]:
        return ""
    ch = (m["now"] - m["prev"]) / abs(m["prev"])
    arrow = "▲" if ch > 0.05 else "▼" if ch < -0.05 else "▶"
    return f"{arrow} {abs(ch):.0%} vs last week"


# ---------- watch list ----------

def watch_items(folder: Path, day: str, rows: list[dict], steps: list[tuple[str, str, str]], tokens: list[dict],
                voice_line: dict, models: dict, cur: dict, history: list[dict], ig: dict, yt: dict, now: datetime,
                settings: dict, series_dir: Path | None = None) -> list[dict]:
    """[{level, title, detail}] - bad first. Only what needs a look gets its own line; passing checks are summed up."""
    s = {**DEFAULTS, **settings}
    items, passed = [], []

    def add(level, title, detail=""):
        items.append({"level": level, "title": title, "detail": detail})

    # 1. did everything post (the old report's whole table, now one line unless something failed)
    failed = [(n, st) for n, st, _ in steps if st not in ("success", "skipped", "")]
    if failed:
        add("bad", "Not posted: " + ", ".join(n for n, _ in failed), "open the run to see why")
    elif steps:
        passed.append("posted")
    # 2. the reel itself: voice check (2026-11-27), TTS fallback (quota)
    if voice_line.get("line"):
        if voice_line.get("warn"):
            line = voice_line["line"]
            if "reel FAILED" in line:
                add("bad", "Reel voice wrong, reel held back", "the voice check failed and no good take could replace it")
            elif "reel REBUILT" in line:
                add("warn", "Reel voice was fixed", "a bad take was rebuilt from the other takes: watch the reel once")
            else:
                add("warn", "A variant's voice was wrong", "that variant was not posted (YouTube got the main reel)")
        else:
            passed.append("voice ok")
    tts = (models or {}).get("voice") or {}
    skipped = [t for t in tts.get("tried", []) if not str(t.get("outcome", "")).startswith("used")]
    if skipped:
        add("warn", "Backup voice model used", f"{tts.get('model')} (Gemini free quota likely used up)")
    # 3. tomorrow's episode (2026-11-23: no file -> the AI pipeline wrote a promo reel instead)
    sd = series_dir or (ROOT / "series")
    try:
        tomorrow = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        if sd.is_dir():
            if (sd / f"{tomorrow}.txt").is_file():
                passed.append("tomorrow's episode ready")
            else:
                add("warn", f"No episode for {date.fromisoformat(tomorrow):%d %b}",
                    f"series/{tomorrow}.txt missing: the AI will write tomorrow's reel")
    except ValueError:
        pass
    # 4. yesterday's reel against the usual
    done = [r for r in rows if r["v24"] is not None]
    if done:
        latest, earlier = done[-1], done[-1 - s["reels_in_chart"]:-1]
        usual = median(r["v24"] for r in earlier)
        if usual:
            ratio = latest["v24"] / usual
            detail = f"{latest['title']}: {latest['v24']:,} views on day 1, usual {usual:,.0f}"
            if ratio < s["flop_ratio"]:
                add("warn", "Yesterday's reel flopped", detail)
            elif ratio >= s["hit_ratio"]:
                add("good", f"Yesterday's reel: {ratio:.1f}× the usual views", detail)
            else:
                passed.append("yesterday's reel normal")
    # 5. followers stalled (2026-11-21: stuck at 24 for days, nobody noticed)
    then = snapshot_near(history, now - timedelta(days=s["flat_followers_days"]))
    f_now, f_then = (cur.get("ig") or {}).get("followers"), ((then or {}).get("ig") or {}).get("followers")
    if f_now is not None and f_then is not None:
        if f_now <= f_then:
            add("warn", f"Followers stuck at {f_now} for {s['flat_followers_days']} days", "")
        else:
            passed.append(f"followers +{f_now - f_then} in {s['flat_followers_days']} days")
    # 6. a profile link nobody taps
    acct = ((ig.get("account") or {}).get("values")) or {}
    if acct.get("reach") and acct.get("reach") > 2000 and not acct.get("profile_links_taps"):
        add("warn", "Nobody tapped the profile link this week",
            f"{acct['reach']:,} people reached, {acct.get('profile_views') or 0} opened the profile")
    # 7. YouTube views per Short (2026-11-21: Shorts fell from 22-132 to 5-14 views after the SEO change)
    vids = sorted((v for v in yt.get("videos") or [] if v.get("published")), key=lambda v: v["published"])
    old = [v for v in vids if v["published"] <= (now - timedelta(days=2)).date().isoformat()]
    if len(old) >= 6:
        a, b = median(int(v.get("viewCount") or 0) for v in old[-3:]), median(int(v.get("viewCount") or 0) for v in old[-10:-3])
        if b and a is not None and a < b * 0.5:
            add("warn", "YouTube views dropped", f"newest Shorts ~{a:,.0f} views each, before ~{b:,.0f}")
        elif b:
            passed.append("YouTube steady")
    # 8. tokens about to expire
    for tk in tokens:
        if tk.get("warn") and tk.get("line"):
            add("bad", "A login token needs renewing", tk["line"][:120])
    # 9. what recent changes said to watch
    for c in recent_changes(now, s["changes_days"]):
        if c.get("watch"):
            add("info", c["watch"], "")
    items.sort(key=lambda i: -RANK[i["level"]])
    return items, passed


# ---------- what changed ----------

def recent_changes(now: datetime, days: int, path: Path | None = None) -> list[dict]:
    """changes.toml entries of the last `days` days, newest first."""
    f = path or (ROOT / "changes.toml")
    if not f.is_file():
        return []
    try:
        entries = tomllib.loads(f.read_text(encoding="utf-8")).get("change", [])
    except tomllib.TOMLDecodeError:
        return []
    cutoff = (now - timedelta(days=days)).date()
    out = []
    for e in entries:
        try:
            d = e["date"] if isinstance(e["date"], date) else date.fromisoformat(str(e["date"]))
        except (KeyError, ValueError):
            continue
        if d >= cutoff:
            out.append({**e, "date": d.isoformat()})
    return sorted(out, key=lambda e: e["date"], reverse=True)


AREAS = [("tools/voice-generator", "voice"), ("tools/caption-maker", "captions"), ("tools/composer", "video render"),
         ("tools/report", "this report"), ("tools/script-writer", "AI scripts"), ("tools/visual-capture", "screenshots"),
         ("tools/instagram-dm", "DM bot"), ("cloudflare/instagram-dm", "DM bot"), ("tools/", "publishing"),
         ("series/", "episode scripts"), (".github/", "nightly workflow"), ("run.py", "pipeline"), ("tests/", "tests")]


def human_commits(since: datetime, repo: Path | None = None) -> list[dict]:
    """Commits not made by the bot (stats/history), with the parts of the pipeline they touched."""
    try:
        r = subprocess.run(["git", "-C", str(repo or ROOT), "log", f"--since={since.isoformat()}", "--no-merges",
                            "--format=@@%h|%an|%ad|%s", "--date=short", "--name-only"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    out, cur = [], None
    for line in r.stdout.splitlines():
        if line.startswith("@@"):
            h, author, d, subject = line[2:].split("|", 3)
            cur = None if author == "reel-bot" or subject.startswith(("stats:", "history:")) else {
                "hash": h, "date": d, "subject": subject, "areas": []}
            if cur:
                out.append(cur)
        elif cur and line.strip():
            area = next((name for prefix, name in AREAS if line.startswith(prefix)), None)
            if area and area not in cur["areas"]:
                cur["areas"].append(area)
    return out


# ---------- charts ----------

def _fig(w: float = 7.2, h: float = 3.0):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(w, h), dpi=150)
    fig.patch.set_facecolor("white")
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=INK2, labelsize=8, length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    return plt, fig, ax


def _png(plt, fig) -> bytes:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def chart_views(rows: list[dict], n: int, flop: float) -> bytes | None:
    rs = [r for r in rows if r["v24"] is not None][-n:]
    if len(rs) < 3:
        return None
    plt, fig, ax = _fig()
    usual = median(r["v24"] for r in rs[:-1]) or 0
    xs = range(len(rs))
    colors = [BAD if r["v24"] < usual * flop else BLUE for r in rs]
    colors[-1] = BLUE_DARK if colors[-1] == BLUE else colors[-1]
    ax.bar(xs, [r["v24"] for r in rs], color=colors, width=0.68, edgecolor="white", linewidth=1.5)
    ax.axhline(usual, color=INK2, linestyle=(0, (4, 3)), linewidth=1.2)
    ax.text(-0.45, usual, f"usual {usual:,.0f}", va="bottom", ha="left", fontsize=8, color=INK2,
            bbox={"facecolor": "white", "edgecolor": "none", "pad": 1})
    top = max(rs, key=lambda r: r["v24"])
    for i, r in enumerate(rs):
        if r is rs[-1] or r is top or colors[i] == BAD:
            label = f"{r['v24']:,}" + (" ▼" if colors[i] == BAD else "")
            ax.text(i, r["v24"], label, ha="center", va="bottom", fontsize=7.5, color=INK)
    ax.set_xticks(list(xs), [r["ts"].strftime("%d %b") for r in rs], rotation=0, fontsize=7)
    ax.set_title("Views on day 1, each reel   (red = flop, dark = newest)", fontsize=10, color=INK, loc="left")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    return _png(plt, fig)


def chart_saves(rows: list[dict], n: int, target: float) -> bytes | None:
    rs = [r for r in rows if r["v24"] and r["ss24"] is not None][-n:]
    if len(rs) < 3:
        return None
    plt, fig, ax = _fig(h=2.6)
    vals = [per_1k(r["ss24"], r["v24"]) for r in rs]
    xs = range(len(rs))
    ax.bar(xs, vals, color=[BLUE] * (len(rs) - 1) + [BLUE_DARK], width=0.68, edgecolor="white", linewidth=1.5)
    ax.axhline(target, color=GOOD, linestyle=(0, (4, 3)), linewidth=1.2)
    ax.text(-0.45, target, f"goal {target:g}", va="bottom", ha="left", fontsize=8, color="#006c10",
            bbox={"facecolor": "white", "edgecolor": "none", "pad": 1})
    best = max(range(len(rs)), key=lambda i: vals[i])
    for i in {best, len(rs) - 1}:
        ax.text(i, vals[i], f"{vals[i]:.0f}", ha="center", va="bottom", fontsize=7.5, color=INK)
    ax.set_xticks(list(xs), [r["ts"].strftime("%d %b") for r in rs], fontsize=7)
    ax.set_title("Saves + shares per 1,000 views, each reel", fontsize=10, color=INK, loc="left")
    return _png(plt, fig)


def chart_followers(history: list[dict], cur: dict, days: int = 30) -> bytes | None:
    """Instagram followers and YouTube subscribers: two small panels on their own axes (never a dual axis)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    pts = [(datetime.fromisoformat(s["at"]), s) for s in history if datetime.fromisoformat(s["at"]) >= since] + \
          [(datetime.now(timezone.utc), cur)]
    series = []
    for label, get in (("Instagram followers", lambda s: (s.get("ig") or {}).get("followers")),
                       ("YouTube subscribers", lambda s: (s.get("yt") or {}).get("subscribers"))):
        xy = [(t, int(v)) for t, s in pts if (v := get(s)) not in (None, "")]
        if len(xy) >= 2:
            series.append((label, xy))
    if not series:
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    fig, axes = plt.subplots(1, len(series), figsize=(7.2, 2.3), dpi=150, squeeze=False)
    fig.patch.set_facecolor("white")
    for ax, (label, xy) in zip(axes[0], series):
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(AXIS)
        ax.tick_params(colors=INK2, labelsize=7.5, length=0)
        ax.yaxis.grid(True, color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.plot([p[0] for p in xy], [p[1] for p in xy], color=BLUE, linewidth=2)
        ax.plot([xy[-1][0]], [xy[-1][1]], "o", color=BLUE, markersize=5)
        ax.annotate(f"{xy[-1][1]}", xy[-1], textcoords="offset points", xytext=(4, 4), fontsize=8, color=INK)
        ax.set_title(f"{label}: {xy[0][1]} → {xy[-1][1]} ({days} days)", fontsize=9.5, color=INK, loc="left")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=5))
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


# ---------- funnel ----------

def funnel(ig: dict, dm: dict) -> list[list[tuple[str, int | None]]]:
    """Two separate paths (a DM is not a subset of profile-link taps, so no rate across them)."""
    acct = ((ig.get("account") or {}).get("values")) or {}
    week = (dm or {}).get("week") or {}
    return [[("Accounts reached", acct.get("reach")), ("Engaged (liked, saved, commented...)", acct.get("accounts_engaged")),
             ("Visited the profile", acct.get("profile_views")), ("Tapped the profile link", acct.get("profile_links_taps"))],
            [("Got a DM after a keyword comment", week.get("dms")), ("Clicked to Telegram from the DM", week.get("tgClicks")),
             ("Joined Telegram via the DM link", week.get("tgJoins"))]]


# ---------- the mail ----------

CHIP_NAMES = (("Reel ready", "Reel"), ("Instagram reel", "Insta"), ("Instagram story", "Story"), ("YouTube Short", "YouTube"),
              ("Instagram trial reel", "Trial"), ("Facebook reel", "Facebook"), ("Threads post", "Threads"),
              ("Telegram post", "Telegram"), ("LinkedIn post", "LinkedIn"))

SHORT_STEP = {"Accounts reached": "reached", "Engaged (liked, saved, commented...)": "liked/saved/commented",
              "Visited the profile": "opened profile", "Tapped the profile link": "tapped link",
              "Got a DM after a keyword comment": "got the DM", "Clicked to Telegram from the DM": "clicked Telegram",
              "Joined Telegram via the DM link": "joined Telegram"}


def chips(steps: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """(short name, icon) per thing that went up tonight; a trial reel the account can't post yet is left out."""
    out = []
    for long, short in CHIP_NAMES:
        st = next((st for n, st, _ in steps if n.startswith(long)), None)
        if st is None or (short == "Trial" and st == "skipped"):
            continue
        out.append((short, "✅" if st == "success" else "➖" if st in ("skipped", "") else "❌"))
    return out


def tile(m: dict) -> str:
    goal = "" if m["target"] is None else f'goal {m["fmt"].format(m["target"])}'
    sub = " · ".join(x for x in (goal, trend(m)) if x)
    icon = ICON[m["status"]] if m["status"] != "info" else ""
    return (f'<td style="width:50%;padding:10px 12px;border:1px solid {GRID};vertical-align:top">'
            f'<div style="font-size:13px;color:{INK2}">{html.escape(m["name"])} '
            f'<span style="font-size:11px">({html.escape(m["how"])})</span></div>'
            f'<div style="font-size:26px;font-weight:bold;color:{INK};margin:2px 0">{fmt(m, m["now"])} '
            f'<span style="font-size:16px">{icon}</span></div>'
            f'<div style="font-size:12px;color:{INK2}">{html.escape(sub)}</div></td>')


def render(folder: Path, day: str, ig: dict, yt: dict, history: list[dict], cur: dict, others: dict,
           steps: list[tuple[str, str, str]], tokens: list[dict], voice_line: dict, models: dict, cfg: dict,
           now: datetime | None = None, series_dir: Path | None = None, charts: bool = True) -> dict:
    """{"level", "headline", "html", "text", "images": {cid: png}}"""
    now = now or datetime.now(timezone.utc)
    settings = {**DEFAULTS, **{k: v for k, v in cfg.items() if k != "targets"}}
    targets = {**TARGETS, **cfg.get("targets", {})}
    rows = reels(ig, history, now)
    items, passed = watch_items(folder, day, rows, steps, tokens, voice_line, models, cur, history, ig, yt, now,
                                settings, series_dir)
    nums = five_numbers(rows, history, cur, ig, others.get("dm") or {}, now, targets)
    level = max((i["level"] for i in items), key=lambda lv: RANK[lv], default="ok")
    worst = next((i for i in items if i["level"] in ("bad", "warn")), None)
    good = next((i for i in items if i["level"] == "good"), None)
    headline = worst["title"] if worst else (good["title"] if good else "all good")

    def h2(txt: str) -> str:
        return f'<p style="font-size:15px;font-weight:bold;margin:22px 0 8px;color:{INK}">{txt}</p>'

    def small(txt: str) -> str:
        return f'<p style="font-size:12px;color:{INK2};margin:4px 0">{txt}</p>'

    h, t = [f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;color:{INK}">'], []

    # Tonight: one row of chips
    cs = chips(steps)
    if cs:
        h.append(h2("Tonight"))
        h.append('<p style="font-size:14px;margin:0;line-height:2.1">' + " &nbsp; ".join(
            f'<span style="white-space:nowrap;border:1px solid {GRID};border-radius:12px;padding:3px 9px">'
            f'{ic} {html.escape(n)}</span>' for n, ic in cs) + "</p>")
        t.append("TONIGHT: " + "  ".join(f"{ic} {n}" for n, ic in cs))

    # Fix: problems only (and the good news), one line each
    fix = [i for i in items if i["level"] in ("bad", "warn", "good")]
    checks = [i for i in items if i["level"] == "info"]
    has_problem = any(i["level"] in ("bad", "warn") for i in fix)
    h.append(h2("Fix" if has_problem else "Nothing to fix"))
    t += ["", "FIX" if has_problem else "NOTHING TO FIX"]
    for i in fix:
        detail = (f'<br><span style="color:{INK2};font-size:12px;margin-left:24px">{html.escape(i["detail"])}</span>'
                  if i["detail"] else "")
        h.append(f'<p style="margin:6px 0;font-size:14px">{ICON[i["level"]]} <b>{html.escape(i["title"])}</b>{detail}</p>')
        t.append(f"{ICON[i['level']]} {i['title']}" + (f" ({i['detail']})" if i["detail"] else ""))
    if passed:
        h.append(small("Fine: " + html.escape(", ".join(passed))))
        t.append("Fine: " + ", ".join(passed))

    # This week: five tiles + one "do this" tile for the biggest gap
    h.append(h2("This week"))
    t += ["", "THIS WEEK"]
    below = [m for m in nums if m["status"] in ("bad", "warn") and m["target"]]
    gap = min(below, key=lambda m: (m["now"] or 0) / m["target"]) if below else None
    cells = [tile(m) for m in nums]
    if gap:
        cells.append(f'<td style="width:50%;padding:10px 12px;border:1px solid {GRID};background:#f9f9f9;vertical-align:top">'
                     f'<div style="font-size:13px;color:{INK2}">Do this week</div>'
                     f'<div style="font-size:14px;margin-top:4px"><b>{html.escape(gap["action"])}</b></div>'
                     f'<div style="font-size:12px;color:{INK2};margin-top:4px">biggest gap: {html.escape(gap["name"].lower())}</div></td>')
    h.append('<table cellpadding="0" cellspacing="6" style="width:100%;border-collapse:separate">')
    for k in range(0, len(cells), 2):
        pair = cells[k:k + 2]
        h.append("<tr>" + "".join(pair) + ("<td></td>" if len(pair) == 1 else "") + "</tr>")
    h.append("</table>")
    for m in nums:
        goal = "" if m["target"] is None else f" (goal {m['fmt'].format(m['target'])})"
        icon = ICON[m["status"]] if m["status"] != "info" else "-"
        t.append(f"{icon} {m['name']}: {fmt(m, m['now'])}{goal} {trend(m)}".rstrip())
    if gap:
        t.append(f"Do this week: {gap['action']}")

    # Charts
    images: dict[str, bytes] = {}
    if charts:
        try:
            for cid, png in (("views24", chart_views(rows, settings["reels_in_chart"], settings["flop_ratio"])),
                             ("saves24", chart_saves(rows, settings["reels_in_chart"], targets["saves_shares_per_1k"])),
                             ("followers", chart_followers(history, cur))):
                if png:
                    images[cid] = png
        except ImportError:
            t.append("(charts need matplotlib)")
    if images:
        h.append(h2("Charts"))
        for cid in images:
            h.append(f'<p style="margin:0 0 12px"><img src="cid:{cid}" width="600" '
                     f'style="max-width:100%;height:auto;display:block" alt="{cid}"></p>')

    # LinkedIn per post (when the Make scenario answers them): one line
    li = others.get("li") or {}
    li_posts = [li["posts"][u] for u in li.get("post_ids") or [] if u in (li.get("posts") or {})]
    if li_posts:
        newest = li_posts[0]
        line = (f"LinkedIn: newest post {newest.get('impressions', 0):,} views, {newest.get('clicks', 0):,} clicks · "
                f"last {len(li_posts)} posts {sum(p.get('impressions', 0) for p in li_posts):,} views")
        h.append(small(html.escape(line)))
        t.append(line)

    # Drop-off: one line of arrows per path
    lines = []
    for path in funnel(ig, others.get("dm") or {}):
        got = [(n, v) for n, v in path if v is not None]
        if got:
            lines.append(" → ".join(f"<b>{v:,}</b> {html.escape(SHORT_STEP.get(n, n))}" + (" ❌" if v == 0 and k else "")
                                    for k, (n, v) in enumerate(got)))
    if lines:
        h.append(h2("Where people drop off (7 days)"))
        h += [f'<p style="font-size:13px;margin:4px 0">{ln}</p>' for ln in lines]
        t += ["", "DROP-OFF (7 days)"] + [re.sub(r"<[^>]+>", "", ln) for ln in lines]

    # Changed lately: what - why, then what to check
    ch = recent_changes(now, settings["changes_days"])
    commits = human_commits(now - timedelta(days=settings["changes_days"]))
    if ch or commits or checks:
        h.append(h2("Changed lately"))
        t += ["", "CHANGED LATELY"]
        for c in ch:
            h.append(f'<p style="font-size:13px;margin:5px 0">• <b>{html.escape(c.get("short") or c["what"])}</b>'
                     f' <span style="color:{INK2}">- {html.escape(c.get("why", ""))}</span></p>')
            t.append(f"• {c.get('short') or c['what']} - {c.get('why', '')}")
        for c in checks:
            h.append(f'<p style="font-size:12px;margin:4px 0;color:{INK2}">ℹ️ Check: {html.escape(c["title"])}</p>')
            t.append(f"Check: {c['title']}")
        if commits:
            areas = sorted({a for c in commits for a in c["areas"]})
            note = f"{len(commits)} code push{'es' if len(commits) > 1 else ''}" + (f" ({', '.join(areas)})" if areas else "")
            h.append(small(html.escape(note)))
            t.append(note)
    h.append(small("All numbers and every post: the attached file."))
    h.append("</div>")
    return {"level": level, "headline": headline, "html": "\n".join(h), "text": t, "images": images}
