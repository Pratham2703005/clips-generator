"""
script-writer: turns a day's brief.json into the dialogue for the reel, using Gemini. See README.md.

    python tools/script-writer/write_script.py [--date YYYY-MM-DD | --brief FILE] [--config FILE] [--model NAME] [--print]

Writes <content_dir>/<date>/script.json (for the next steps) and script.md (to read and judge).
Every script is checked before it is saved: line and word counts, both characters speak, every job is mentioned,
job ids exist, no number that isn't in the brief, the last line is the call to action. A failing script is sent back
with the list of problems; after `attempts` tries the next model in the config is used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"
SECTION = "script"  # this tool's table in the master config and the mode files

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

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def load_env_files(files: list[Path]) -> None:
    for f in files:
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---- prompt ----

def response_schema(brief: dict) -> dict:
    speakers = [c["id"] for c in brief["cast"]]
    return {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING", "description": "Internal title for this episode, under 8 words"},
            "lines": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "speaker": {"type": "STRING", "enum": speakers},
                        "text": {"type": "STRING"},
                        "expression": {"type": "STRING", "enum": ["normal", "shocked"]},
                        "job_id": {"type": "STRING", "description": "id of the job this line is about, or empty"},
                    },
                    "required": ["speaker", "text", "expression"],
                },
            },
            "hook_card": {"type": "STRING", "description": "On-screen text for the first 3 seconds: the hook's claim in at most 6 words, e.g. 'THE JOB CLOSED 3 WEEKS AGO.'"},
            "caption": {"type": "STRING", "description": "1-2 sentence Instagram caption hook, no hashtags"},
            "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["title", "lines", "hook_card", "caption", "hashtags"],
    }


def build_prompt(brief: dict, cfg: dict) -> tuple[str, str]:
    s = cfg["script"]
    system = "\n".join(f"- {r}" for r in cfg["style"]["rules"]) + (
        f"\n- {s['min_lines']} to {s['max_lines']} lines, at most {s['max_words_per_line']} words per line, "
        f"{s['min_words']} to {s['max_words']} words in total. Both characters speak and take turns: nobody says more than "
        f"{s['max_run']} lines in a row, and each has at least {round(s['min_share'] * 100)}% of the lines."
        f"\n- Lines about a specific job (with job_id) are at most {round(s['max_data_share'] * 100)}% of the words."
        f"\n- {s['hashtags_min']} to {s['hashtags_max']} hashtags, without the # sign."
    )
    if cfg["style"].get("example"):
        system += "\n\nExample of the tone (rhythm and teasing only; do not reuse its facts or numbers):\n" + cfg["style"]["example"].strip()
    cast = [{k: c.get(k) for k in ("id", "name", "nickname", "position", "persona", "catchphrase", "running_gag") if c.get(k)} for c in brief["cast"]]
    jobs = [{k: j[k] for k in ("id", "title", "company", "where", "posted", "experience") if j.get(k)} for j in brief["jobs"]]
    payload = {
        "date": brief["date"],
        "track": brief["track"],
        "format": {"id": brief["format"]["id"], "instructions": brief["format"]["instructions"]},
        "niche": {k: brief["niche"].get(k) for k in ("label", "audience")},
        "cast": cast,
        # the left character is the one with the answer (casts are listed [left, right] in content-data-preparation)
        "roles": {
            "knows": next(c["name"] for c in brief["cast"] if c.get("position") == "left"),
            "learns": next(c["name"] for c in brief["cast"] if c.get("position") == "right"),
        },
        "jobs": jobs,
        "spotlight": brief.get("spotlight"),
        "stats": brief["stats"],
        # what each stat means, so a script doesn't call 426,528 new postings "jobs updated this week" (2026-10-29)
        "stats_meaning": {
            "total_jobs": "live jobs in the whole index right now",
            "companies": "companies those live jobs come from",
            "posted_last_7_days": "new jobs posted in the last 7 days",
            "remote_jobs": "live remote jobs",
            "niche.live_jobs": "live jobs in this niche's category, all countries (not only today's)",
        },
        "platform": {k: brief["platform"].get(k) for k in ("name", "what", "for", "how_to_find", "cta")},
        "product_facts": brief["facts"],
        "context_facts": brief.get("context", []),
        # today's idea from banks.toml, so reels don't repeat one opening (2026-10-30)
        **{k: brief[k] for k in ("premise", "hook", "dynamic") if brief.get(k)},
    }
    user = "Write today's reel from this brief. Use only these facts.\n\n" + json.dumps(payload, indent=2, ensure_ascii=False)
    return system, user


# ---- checks ----

NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
SCALES = {"hundred": 100, "thousand": 1_000, "k": 1_000, "lakh": 100_000, "million": 1_000_000, "m": 1_000_000, "crore": 10_000_000}


def spoken_numbers(text: str) -> list[tuple[str, float]]:
    """Numbers as a voice would say them: "20,000", "20K", "1.9 million", "twenty thousand", "four hundred thousand"."""
    toks = re.findall(r"\d[\d,]*(?:\.\d+)?|[a-z]+", text.lower())
    out, i = [], 0
    while i < len(toks):
        t = toks[i]
        if t[0].isdigit():
            v, phrase = float(t.replace(",", "")), t
            while i + 1 < len(toks) and toks[i + 1] in SCALES:  # "1.9 million", "20 k"
                i += 1
                v *= SCALES[toks[i]]
                phrase += " " + toks[i]
            out.append((phrase, v))
        elif t in UNITS or t in TENS:
            total, current, words = 0.0, 0.0, []
            while i < len(toks) and (toks[i] in UNITS or toks[i] in TENS or toks[i] in SCALES or (toks[i] == "and" and words)):
                w = toks[i]
                if w in UNITS or w in TENS:
                    current += UNITS.get(w, 0) + TENS.get(w, 0)
                elif w == "hundred":
                    current *= 100
                elif w in SCALES:
                    total += max(current, 1) * SCALES[w]
                    current = 0
                words.append(w)
                i += 1
            out.append((" ".join(words), total + current))
            continue
        i += 1
    return out


def allowed_numbers(brief: dict, free: list[int]) -> set[str]:
    """Every number in the brief, plus honest roundings of the big ones (1,974,704 -> 1.9 / 1.97 / 2 million, 1974 thousand)."""
    found: set[float] = set(float(n) for n in free)

    def walk(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            found.add(float(x))
        elif isinstance(x, str):
            for m in NUM.findall(x):
                try:
                    found.add(float(m.replace(",", "")))
                except ValueError:
                    pass
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(brief)
    out: set[str] = set()
    for v in found:
        out.add(f"{v:g}")
        if v >= 1000:
            for div, decimals in ((1e3, 0), (1e6, 0), (1e6, 1), (1e6, 2)):
                if v >= div:
                    for r in (round(v / div, decimals), int(v / div * 10**decimals) / 10**decimals):
                        out.add(f"{r:g}")
            for step in (1e3, 1e4, 1e5, 1e6):  # "over 400 thousand", "almost 2 million" as whole numbers
                out.add(f"{round(v / step) * step:g}")
                out.add(f"{int(v / step) * step:g}")
    return out


HINDI_WORDS = set("""
hai hain ho hoon tha thi the hum tum aap main mai mujhe tujhe tumhe humein unhe use usko isko kya kyun kaise kahan kab
kaun koi kuch sab bhi hi toh nahi nahin na mat aur ya pe ka ki ke ko se mein tak wala wali wale yeh ye woh wo yahan
wahan ab abhi phir fir bas sirf bahut bohot accha acha theek thik chal chalo dekh dekho bol bolo kar karo karna karte
karti kiya kiye raha rahi rahe gaya gayi gaye hota hoti hote hua hui jaata jaati jao ja de do dena le lo lena mil mila
mili milta milegi apna apni apne mera meri mere tera teri tere uska uski unka unki yaar arre arey beta bhai matlab
lekin magar kyunki agar warna jab tab jaise waise itna utna kitna kitne sakta sakti sakte chahiye pata hoga hogi lagta
lagti samjho samjha naukri
""".split())


def hindi_share(text: str) -> float:
    """Share of the words that are common Hindi words: Roman-script Hinglish scores ~0.35, English ~0.05."""
    w = re.findall(r"[a-z']+", text.lower())
    return sum(x in HINDI_WORDS for x in w) / max(len(w), 1)


# Premises (content-data-preparation/banks.toml) about piling up applications, where the gag may open the reel.
GAG_PREMISES = {"apply-everywhere", "spreadsheet-tracker", "hr-inside-view", "ghosted-after-apply"}


def check(script: dict, brief: dict, cfg: dict) -> list[str]:
    s, problems = cfg["script"], []
    lines = script.get("lines") or []
    speakers = {c["id"] for c in brief["cast"]}
    if not (s["min_lines"] <= len(lines) <= s["max_lines"]):
        problems.append(f"{len(lines)} lines; need {s['min_lines']}-{s['max_lines']}.")
    words = 0
    for i, ln in enumerate(lines, 1):
        n = len(ln.get("text", "").split())
        words += n
        if n > s["max_words_per_line"]:
            style = "(style) " if n <= s.get("soft_max_words_per_line", s["max_words_per_line"]) else ""
            problems.append(f"{style}line {i} has {n} words; max {s['max_words_per_line']}.")
        if ln.get("speaker") not in speakers:
            problems.append(f"line {i}: unknown speaker {ln.get('speaker')!r}.")
        if re.search(r"https?://|www\.|#\w", ln.get("text", "")):
            problems.append(f"line {i}: no URLs or hashtags in spoken lines.")
    if not (s["min_words"] <= words <= s["max_words"]):
        style = "(style) " if s.get("soft_min_words", s["min_words"]) <= words <= s.get("soft_max_words", s["max_words"]) else ""
        problems.append(f"{style}{words} words in total; need {s['min_words']}-{s['max_words']}.")
    if len({ln.get("speaker") for ln in lines}) < 2:
        problems.append("both characters must speak.")
    run, longest = 0, 0
    for k, ln in enumerate(lines):
        run = run + 1 if k and ln.get("speaker") == lines[k - 1].get("speaker") else 1
        if run == s["max_run"] + 1:
            problems.append(f"(style) line {k + 1}: {ln.get('speaker')} speaks more than {s['max_run']} lines in a row; let the other character react.")
    for sp in speakers:
        share = sum(1 for ln in lines if ln.get("speaker") == sp) / max(len(lines), 1)
        if share < s["min_share"]:
            # a near miss (30% vs 35%) is style; a monologue is not
            style = "(style) " if share >= s["min_share"] - 0.1 else ""
            problems.append(f"{style}{sp} has only {share:.0%} of the lines; needs at least {s['min_share']:.0%}.")
    share = hindi_share(" ".join(ln.get("text", "") for ln in lines))
    if words and share < s.get("min_hindi_share", 0):
        problems.append(f"the script is in English ({share:.0%} Hindi words, need {s['min_hindi_share']:.0%}+); write natural "
                        "Hinglish in Roman script: 'Yaar, yeh job toh teen hafte pehle hi band ho gayi thi.'")
    data = sum(len(ln.get("text", "").split()) for ln in lines if ln.get("job_id"))
    if words and data / words > s["max_data_share"]:
        problems.append(f"job lines are {data / words:.0%} of the words; keep them under {s['max_data_share']:.0%} and give more room to story and banter.")
    job_ids = {j["id"] for j in brief["jobs"]}
    used = {ln.get("job_id") for ln in lines if ln.get("job_id")}
    for bad in used - job_ids:
        problems.append(f"job_id {bad!r} is not in the brief.")
    for j in brief["jobs"]:
        if j["id"] not in used:
            problems.append(f"the job at {j['company']} ({j['id']}) is never mentioned with its job_id.")
    ok = allowed_numbers(brief, s["free_numbers"])
    text = " ".join(ln.get("text", "") for ln in lines) + " " + script.get("caption", "")
    for phrase, v in spoken_numbers(text):
        if f"{v:g}" not in ok:
            problems.append(f"the number '{phrase}' ({v:g}) is not in the brief; use only the brief's numbers, rounded down honestly.")
    if lines:
        last = lines[-1].get("text", "").lower()
        cta = brief["platform"]["cta"].lower()
        # the bio holds only the website; job and product links are in the description (user, 2026-11-01)
        where = "description" if brief["jobs"] or "description" in cta else "bio"
        if where not in last:
            problems.append(f"the last line must be the call to action and say the link is in the {where} "
                            f"({'jobs ke links description mein' if brief['jobs'] else brief['platform']['cta']}).")
        knows = next((c["id"] for c in brief["cast"] if c.get("position") == "left"), None)
        if knows and lines[-1].get("speaker") != knows:
            problems.append(f"(style) the call to action (last line) must be said by {knows}, the one who knows.")
    for i, ln in enumerate(lines, 1):
        t = re.sub(r"[*_\"'‘’“”]", "", ln.get("text", "").lower())  # markdown stars and quotes hid "applied to *all* 405,000"
        # index stats turned into a character's own applications ("You applied to 2 million jobs", 2026-10-30)
        if re.search(r"appl(y|ied|ying)\w*\s+(to|for)?\s*(all\s+)?(of\s+)?(the\s+)?[\d,.]+\s*(k|thousand|lakh|million|m)?\b", t) and \
                any(abs(v) >= 1000 for _, v in spoken_numbers(t)):
            problems.append(f"line {i}: a character can't have applied to an index number (those are jobs in the index, not anyone's applications).")
        # never say a featured job is closed or gone: it was checked live (Riya called ZoomInfo's open job 'closed')
        # the company's first word too: "ZoomInfo" for "ZoomInfo Technologies"
        if ln.get("job_id") or any(j["company"].lower().split()[0] in t for j in brief["jobs"] if j["company"].split()):
            if re.search(r"\b(closed|expired|filled|gone|taken down|no longer)\b", t):
                problems.append(f"line {i}: don't say a featured job is closed or gone; every job in the brief is open (checked live).")
    shocked = sum(1 for ln in lines if ln.get("expression") == "shocked")
    if shocked > 2:
        problems.append(f"(style) {shocked} lines are marked shocked; at most 2, for the real shock moments.")
    tags = script.get("hashtags") or []
    if not (s["hashtags_min"] <= len(tags) <= s["hashtags_max"]):
        problems.append(f"{len(tags)} hashtags; need {s['hashtags_min']}-{s['hashtags_max']}.")
    card = (script.get("hook_card") or "").split()
    if not card or len(card) > 6:
        problems.append(f"hook_card has {len(card)} words; it needs 1-6 (the hook's claim, read at a glance).")
    # Bunty's "200 applications" running gag opened all three workflow reels of 2026-10-31/11-01 (and every reel before
    # the idea banks). It may lead only when the premise is about piling up applications.
    gag = re.compile(r"\b(200|two hundred|do sau)\b[^.?!]{0,30}\b(applications?|apply|applied|jagah)\b|\b(applications?|applied)\b[^.?!]{0,20}\b(200|two hundred)\b", re.I)
    premise = (brief.get("premise") or {}).get("id", "")
    opening = f"{script.get('hook_card', '')} {lines[0].get('text', '') if lines else ''}"
    if gag.search(opening) and premise not in GAG_PREMISES:
        problems.append("the hook uses the running gag (200 applications, zero calls); open on today's premise instead "
                        f"({premise or 'see premise'}), the gag may come back once later.")
    if lines:
        problems += quality_checks(lines, cfg)
    return problems


# Problems that are about style, not facts: a script with only these can be used when nothing passes.
SOFT = ("(style)", "(judge)", "the hook (line 1) scores", "beat check:")

JUDGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "score": {"type": "INTEGER", "description": "overall 1-10: would a 20-something Indian viewer watch to the end and send it to a friend"},
        "funny": {"type": "INTEGER", "description": "1-10"},
        "coherent": {"type": "INTEGER", "description": "1-10: roles, logic, every line follows from the last"},
        "issues": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "the most important fixes, each one concrete, best first"},
    },
    "required": ["score", "funny", "coherent", "issues"],
}


def judge(script: dict, brief: dict, cfg: dict, keys: dict, writer: str, log) -> dict | None:
    """Scores a draft with another model (a strict comedy-writing editor). None if no judge model answered."""
    j = cfg.get("judge", {})
    if not j.get("enabled", True):
        return None
    cast = {c["id"]: c for c in brief["cast"]}
    knows = next((c["name"] for c in brief["cast"] if c.get("position") == "left"), "")
    learns = next((c["name"] for c in brief["cast"] if c.get("position") == "right"), "")
    dialogue = "\n".join(f"{cast.get(ln.get('speaker'), {}).get('name', ln.get('speaker'))}"
                         f"{' (shocked)' if ln.get('expression') == 'shocked' else ''}: {ln.get('text', '')}" for ln in script.get("lines", []))
    system = j["rubric"]
    user = (f"Characters:\n" + "\n".join(f"- {c['name']}: {c.get('persona', '')} Catchphrase: {c.get('catchphrase', '')}" for c in brief["cast"])
            + f"\nRoles: {knows} knows the answer and roasts; {learns} has the problem and learns.\n"
            f"Format: {brief['format']['instructions']}\nOn-screen hook: {script.get('hook_card', '')}\n\nScript:\n{dialogue}")
    messages = [{"role": "user", "parts": [{"text": user}]}]
    # a different provider than the writer when possible: it doesn't share the writer's blind spots or rate limit
    order = sorted(j.get("models", []), key=lambda m: (m.split(":")[0] if ":" in m else "gemini") == (writer.split(":")[0] if ":" in writer else "gemini"))
    for m in order:
        provider = m.split(":")[0] if ":" in m else "gemini"
        if not keys.get(provider):
            continue
        for _ in range(4):  # resets on this network are common; a judge call is cheap
            try:
                v = call_model(m, keys, system, messages, JUDGE_SCHEMA, cfg)
                return {"score": int(v.get("score", 0)), "issues": [str(i) for i in v.get("issues", [])], "by": m}
            except Exception as e:
                if not re.search(r"10054|ConnectionReset|RemoteDisconnected|EOF occurred", repr(e)):
                    log(f"judge {m}: {e!r}"[:160])
                    break
                time.sleep(1)
    return None


def run_skill_tool(path: str, args: list[str]) -> dict | None:
    """Runs one of the ig-reel skill's scorers (hookscore.py, beats.py) for its JSON; None if it isn't installed."""
    tool = Path(os.path.expanduser(path)) if path else None
    if not tool or not tool.exists():
        return None
    # PYTHONIOENCODING: on Windows the child would print in cp1252 and curly quotes break the UTF-8 read
    r = subprocess.run([sys.executable, str(tool), "--json", *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        return json.loads(r.stdout or "")
    except json.JSONDecodeError:
        return None


def quality_checks(lines: list[dict], cfg: dict) -> list[str]:
    """The ig-reel skill's own scorers, so a script that would fail the review is sent back before it's voiced
    (2026-10-30: the 41.8 s reel scored 63 on the hook, with no loop and empty middle beats)."""
    q, problems = cfg.get("quality", {}), []
    hook = lines[0].get("text", "")
    hs = run_skill_tool(q.get("hookscore", ""), ["--hook", hook])
    if hs and hs.get("score", 100) < q.get("min_hook_score", 0):
        weak = hs.get("weakest", "")
        problems.append(f"the hook (line 1) scores {hs['score']:.0f}/100, need {q['min_hook_score']}; weakest: {weak} "
                        f"({hs['checks'].get(weak, {}).get('detail', '')}). Say it to 'you', with a number and tension, 5-12 words.")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("\n".join(ln.get("text", "") for ln in lines) + "\n")
    try:
        bs = run_skill_tool(q.get("beats", ""), ["--wpm", str(q.get("wpm", 204)), f.name])
    finally:
        os.unlink(f.name)
    for note in (bs or {}).get("notes", []):
        if re.match(r"(No loop|Beats? .* nothing concrete|Beat\(s\) .* run past)", note):
            problems.append("beat check: " + note)
    return problems


# ---- Gemini ----

def with_deadline(fn, seconds: float):
    """urlopen's timeout is per socket read, so a slow trickle can run for many minutes (gemini-3.8-flash hung 10+ min,
    2026-10-29). This caps the whole call; a call that runs over is left behind in a daemon thread."""
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


def call_gemini(model: str, key: str, system: str, messages: list[dict], schema: dict, cfg: dict) -> dict:
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": messages,
        "generationConfig": {"temperature": cfg["llm"]["temperature"], "responseMimeType": "application/json", "responseSchema": schema},
    }
    req = urllib.request.Request(
        API.format(model=model), data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key, "User-Agent": "video-generator/1.0 (script-writer)"},
    )
    data = with_deadline(lambda: json.loads(urllib.request.urlopen(req, timeout=cfg["llm"]["timeout_seconds"]).read().decode("utf-8")),
                         cfg["llm"]["timeout_seconds"])
    parts = data["candidates"][0]["content"]["parts"]
    return json.loads("".join(p.get("text", "") for p in parts if not p.get("thought")))


def lower_types(schema):
    """Gemini's schema types are upper case (OBJECT, STRING); JSON Schema wants lower case."""
    if isinstance(schema, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else lower_types(v)) for k, v in schema.items()}
    if isinstance(schema, list):
        return [lower_types(v) for v in schema]
    return schema


def call_openai_compatible(base_url: str, model: str, key: str, system: str, messages: list[dict], schema: dict, cfg: dict,
                           extra: dict | None = None) -> dict:
    """Any OpenAI-style chat API (Groq). The same messages as Gemini, with 'model' turns as 'assistant'.
    `extra` is the provider's own body fields (token limits)."""
    chat = [{"role": "system", "content": system}] + [
        {"role": "assistant" if m["role"] == "model" else "user", "content": "".join(p["text"] for p in m["parts"])} for m in messages
    ]
    body = {
        "model": model, "messages": chat, "temperature": cfg["llm"]["temperature"],
        "response_format": {"type": "json_schema", "json_schema": {"name": "script", "schema": lower_types(schema)}},
        **(extra or {}),
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "video-generator/1.0 (script-writer)"},
    )
    data = with_deadline(lambda: json.loads(urllib.request.urlopen(req, timeout=cfg["llm"]["timeout_seconds"]).read().decode("utf-8")),
                         cfg["llm"]["timeout_seconds"])
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        # reasoning models (Groq's gpt-oss) spend tokens thinking first; out of tokens = cut-off or empty JSON
        raise ValueError("reply cut off (finish_reason=length); raise the provider's token limit")
    return json.loads(choice["message"]["content"])


def call_model(spec: str, keys: dict, system: str, messages: list[dict], schema: dict, cfg: dict) -> dict:
    """spec is 'gemini-3.5-flash' (Gemini) or '<provider>:<model>' for a provider in [llm.providers]."""
    provider, _, model = spec.partition(":") if ":" in spec else ("gemini", "", spec)
    if provider == "gemini":
        return call_gemini(model, keys["gemini"], system, messages, schema, cfg)
    p = cfg["llm"]["providers"][provider]
    return call_openai_compatible(p["base_url"], model, keys[provider], system, messages, schema, cfg, p.get("extra"))


def generate(brief: dict, cfg: dict, keys: dict, models: list[str], log) -> tuple[dict, str]:
    system, user = build_prompt(brief, cfg)
    schema = response_schema(brief)
    best = None  # (script, model, problems): the best draft whose problems are all style-only
    # the whole step has a budget: on GitHub (2026-10-30) Gemini sat 24+ min between 503s and quota 429s
    deadline = time.time() + cfg["llm"].get("max_minutes", 12) * 60
    for model in models:
        if time.time() > deadline:
            log(f"out of time ({cfg['llm'].get('max_minutes', 12)} min); not trying {model} and the rest")
            break
        provider = model.partition(":")[0] if ":" in model else "gemini"
        if not keys.get(provider):
            log(f"{model}: skipped, no API key for {provider}")
            continue
        messages = [{"role": "user", "parts": [{"text": user}]}]
        attempt, waits, busy = 0, 0, 0
        # fast models get more tries (flash-lite answers in ~3 s, gpt-oss in ~10 s)
        tries = cfg["llm"].get("attempts_per_model", {}).get(model, cfg["llm"]["attempts"])
        while attempt < tries and time.time() < deadline:
            attempt += 1
            started = time.time()
            log(f"{model}: attempt {attempt}, asking (up to {cfg['llm']['timeout_seconds']} s)")
            try:
                script = call_model(model, keys, system, messages, schema, cfg)
            except urllib.error.HTTPError as e:
                detail = e.read()[:4000].decode("utf-8", "replace")  # Gemini names the quota (…PerDay…) far down the body
                log(f"{model}: HTTP {e.code} {detail.strip()[:200]}")
                if e.code == 429 and re.search(r"PerDay|per day|daily", detail, re.I):
                    log(f"{model}: daily quota used up, trying the next model")
                    break
                if e.code in (500, 503) and busy < len(cfg["llm"].get("busy_backoff", [2, 4, 8])):
                    # "high demand" (Gemini's free tier at Indian peak hours): back off 2 -> 4 -> 8 s without spending
                    # an attempt, then move to the next model
                    delay = cfg["llm"].get("busy_backoff", [2, 4, 8])[busy]
                    busy += 1
                    attempt -= 1
                    log(f"{model}: busy, retrying in {delay} s")
                    time.sleep(delay)
                    continue
                if e.code in (500, 503):
                    break
                if e.code == 429 and waits < cfg["llm"].get("rate_limit_waits", 2):
                    # per-minute limit (Groq free: 8000 tokens/min on gpt-oss-120b); wait as told, without using up an attempt
                    waits += 1
                    attempt -= 1
                    delay = min(float(e.headers.get("retry-after") or 30), 90)
                    log(f"{model}: rate limited, waiting {delay:.0f} s")
                    time.sleep(delay + 1)
                    continue
                # json_validate_failed: Groq's reply didn't fit the schema this time; a retry usually does
                if "json_validate_failed" in detail and attempt < tries:
                    time.sleep(10 * attempt)
                    continue
                break  # 404 (no such model), 400, or out of retries: next model
            except TimeoutError as e:  # a hung model tends to stay hung: next model
                log(f"{model}: attempt {attempt}: {e}")
                break
            except Exception as e:  # dropped connections (RemoteDisconnected, resets), timeouts, malformed replies
                # this network resets ~40% of TLS connections for a moment (curl and Python alike, 2026-10-30):
                # retry those quickly without spending an attempt
                if re.search(r"10054|ConnectionReset|RemoteDisconnected|EOF occurred", repr(e)) and waits < 8:
                    waits += 1
                    attempt -= 1
                    log(f"{model}: connection reset, retrying")
                    time.sleep(1)
                    continue
                log(f"{model}: attempt {attempt} failed: {e!r}")
                time.sleep(5)
                continue
            problems = check(script, brief, cfg)
            log(f"{model}: attempt {attempt} ({time.time() - started:.0f} s), {len(problems)} problem(s)" + (": " + " | ".join(problems) if problems else ""))
            # style-only misses (hook score, beat timing) are kept as a fallback; any fact, number, length or job
            # problem never is (2026-10-30: every model missed the hook score on a Riya + Verma brief and the run died)
            if all(p.startswith(SOFT) for p in problems):
                # a draft that passes the rules can still be bad (2026-10-30: flash-lite passed with "You applied 2
                # times", the learner giving the CTA; the user scored it 3/10), so another model judges it
                verdict = judge(script, brief, cfg, keys, model, log)
                score = verdict["score"] if verdict else None
                if score is not None:
                    log(f"{model}: judge {score}/10" + (": " + " | ".join(verdict.get("issues", [])[:4]) if verdict.get("issues") else ""))
                rank = (score if score is not None else 0, -len(problems))
                if best is None or rank > best[3]:
                    best = (script, model, problems, rank)
                if not problems and (score is None or score >= cfg.get("judge", {}).get("min_score", 7)):
                    log(f"{model}: accepted on attempt {attempt}")
                    return script, model
                if verdict and score is not None and score < cfg.get("judge", {}).get("min_score", 7):
                    problems += [f"(judge) {i}" for i in verdict.get("issues", [])[:5]] or ["(judge) make it funnier and more coherent"]
            messages += [
                {"role": "model", "parts": [{"text": json.dumps(script, ensure_ascii=False)}]},
                {"role": "user", "parts": [{"text": "Fix these problems and return the whole script again:\n- " + "\n- ".join(problems)}]},
            ]
    if best:
        log(f"WARNING: no script passed everything; using the best judged draft ({best[1]}, judge {best[3][0]}/10)"
            + (", style misses: " + " | ".join(best[2]) if best[2] else ""))
        return best[0], best[1]
    sys.exit("no model produced a script that passed the checks")


# ---- output ----

def finish(script: dict, brief: dict, model: str) -> dict:
    """Adds what the next steps need; the caption gets a fact list built from the brief, not from the model."""
    names = {c["id"]: c["name"] for c in brief["cast"]}
    jobs = {j["id"]: j for j in brief["jobs"]}
    tags = []
    for t in script["hashtags"]:
        t = re.sub(r"[^\w]", "", t.lstrip("#"))
        if t and t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    # the description carries every link (the bio has only the website): each job's apply page, then the product
    picks = "\n".join(f"- {j['title']} at {j['company']} ({j['where']}, posted {j['posted']})\n  Apply: {j['apply_url']}" for j in brief["jobs"])
    caption = script["caption"].strip()
    if picks:
        caption += f"\n\nJobs in this video:\n{picks}"
    platform = brief["platform"]
    if platform.get("url") and platform.get("id") != "landing":
        caption += f"\n\n{platform['name'][:1].upper() + platform['name'][1:]}: {platform['url']}"
    caption += "\n\nMore fresh jobs from company career pages: link in bio."
    caption += "\n\n" + " ".join(f"#{t}" for t in tags)
    return {
        "date": brief["date"],
        "title": script["title"],
        "track": brief["track"],
        "format": brief["format"]["id"],
        "cast": brief["cast"],
        "lines": [
            {
                "speaker": ln["speaker"],
                "name": names[ln["speaker"]],
                "text": ln["text"].strip(),
                "expression": ln.get("expression", "normal"),
                "job_id": ln.get("job_id") or None,
                "job": {k: jobs[ln["job_id"]][k] for k in ("title", "company", "where", "posted")} if ln.get("job_id") in jobs else None,
            }
            for ln in script["lines"]
        ],
        "hook_card": (script.get("hook_card") or "").strip(),
        "caption": caption,
        "hashtags": tags,
        "words": sum(len(ln["text"].split()) for ln in script["lines"]),
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def as_markdown(s: dict) -> str:
    # ~3.4 words per second once the voice is sped up to 1.25x (2026-10-30: 141 words -> 41.0 s)
    out = [f"# {s['title']}", "", f"{s['date']} · {s['track']} · {s['format']} · {s['words']} words (~{round(s['words'] / 3.4)} s) · {s['model']}", "",
           f"On screen, first 3 s: **{s.get('hook_card', '')}**", ""]
    for ln in s["lines"]:
        tag = f"  _[{ln['job']['company']}]_" if ln["job"] else ""
        face = " (shocked)" if ln["expression"] == "shocked" else ""
        out.append(f"**{ln['name']}**{face}: {ln['text']}{tag}  ")
    out += ["", "## Caption", "", s["caption"], ""]
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser(description="Write the reel's dialogue from a brief.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--date", help="YYYY-MM-DD (default: the latest brief)")
    p.add_argument("--brief", type=Path, help="a brief.json anywhere (output goes next to it)")
    p.add_argument("--model", help="use only this model")
    p.add_argument("--print", action="store_true", help="also print the script")
    p.add_argument("--draft", type=Path, help="a hand-written script JSON (title, lines, hook_card, caption, hashtags): "
                                              "checked and finished like a model's, no model called (for testing)")
    args = p.parse_args()

    cfg = load_config(args.config)
    base = args.config.resolve().parent
    load_env_files([(base / f).resolve() for f in cfg["llm"].get("env_files", [])])
    keys = {"gemini": os.environ.get(cfg["llm"]["key_env"], "").strip()}
    for name, prov in cfg["llm"].get("providers", {}).items():
        keys[name] = os.environ.get(prov["key_env"], "").strip()
    if not any(keys.values()):
        sys.exit(f"no API key: set {cfg['llm']['key_env']} (or a provider's key_env) in the environment or in {cfg['llm'].get('env_files')}")

    content = (base / cfg["paths"]["content_dir"]).resolve()
    if args.brief:
        brief_path = args.brief.resolve()
    elif args.date:
        brief_path = content / args.date / "brief.json"
    else:
        days = sorted(d for d in content.iterdir() if (d / "brief.json").exists()) if content.exists() else []
        if not days:
            sys.exit(f"no briefs in {content}; run content-data-preparation first")
        brief_path = days[-1] / "brief.json"
    if not brief_path.exists():
        sys.exit(f"brief not found: {brief_path}")
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    log(f"brief: {brief_path} ({brief['track']} / {brief['format']['id']} / {', '.join(c['name'] for c in brief['cast'])})")

    if args.draft:
        script, model = json.loads(args.draft.read_text(encoding="utf-8")), "hand-written"
        problems = check(script, brief, cfg)
        hard = [x for x in problems if not x.startswith(SOFT)]
        for x in problems:
            log(("HARD: " if x in hard else "style: ") + x)
        if hard:
            sys.exit("the draft breaks hard rules; fix it first")
    else:
        script, model = generate(brief, cfg, keys, [args.model] if args.model else cfg["llm"]["models"], log)
    final = finish(script, brief, model)
    (brief_path.parent / "script.json").write_text(json.dumps(final, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md = as_markdown(final)
    (brief_path.parent / "script.md").write_text(md, encoding="utf-8")
    log(f"wrote {brief_path.parent / 'script.json'} and script.md")
    if args.print:
        sys.stdout.reconfigure(encoding="utf-8")  # the Windows console default (cp1252) mangles "·"
        print(md)


if __name__ == "__main__":
    main()
