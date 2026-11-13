"""
Make a reel in one command. See README.md.

    python run.py ats [--date YYYY-MM-DD]                    the daily ATS Jobs API reel (modes/ats.toml)
    python run.py custom [SCRIPT.txt] [options]              your own script and choices (modes/custom.toml)
    python run.py path/to/any-mode.toml [options]

Options (each one overrides the mode file's [run] setting):
    --left ID / --right ID     characters (left knows, right learns)
    --hook "TEXT"              on-screen hook card for your own script
    --visuals on|off           screenshots of real pages
    --clip clip2.mp4           background clip
    --name NAME                output folder name for your own script
    --date YYYY-MM-DD          the day (default: today)
    --script-text "A: .. | B: .."   your script in one line instead of a file
    --track / --niche / --format / --platform   AI script: pick these instead of the usual draw
    --set KEY=VALUE            any setting for this run only: compose.panel.scroll=120; voice.voice.target_wps=3.6

Settings layer up: each tool's own config.toml < config.toml (master) < the mode file < --set. This script only runs
the tools in order with the mode file in $VG_MODE_FILE and the --set values in $VG_SET_FILE; every tool still works
on its own.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
STEP = {
    "brief": TOOLS / "content-data-preparation/prepare_content.py",
    "script": TOOLS / "script-writer/write_script.py",
    "voice": TOOLS / "voice-generator/generate_voice.py",
    "captions": TOOLS / "caption-maker/make_captions.py",
    "visuals": TOOLS / "visual-capture/capture.py",
    "compose": TOOLS / "composer/compose.py",
}


def run(step: str, args: list[str], env: dict) -> None:
    print(f"\n== {step}", flush=True)
    r = subprocess.run([sys.executable, "-u", str(STEP[step]), *args], env=env)
    if r.returncode != 0:
        sys.exit(f"{step} failed (exit {r.returncode}); the files made so far are kept")


def characters() -> dict:
    """Characters as the content tool sees them (its config + master + mode), plus the voice labels for matching."""
    sys.path.insert(0, str(TOOLS / "content-data-preparation"))
    import prepare_content as pc
    chars = pc.load_config(pc.DEFAULT_CONFIG)["characters"]
    sys.path.insert(0, str(TOOLS / "voice-generator"))
    import generate_voice as gv
    voices = gv.load_config(gv.DEFAULT_CONFIG)["characters"]
    for cid, c in chars.items():
        c["label"] = voices.get(cid, {}).get("label", c["name"])
    return chars


def find_char(name: str, chars: dict) -> str | None:
    """'Malhotra', "Malhotra Ma'am", 'malhotra' or the voice label all find the same character."""
    n = re.sub(r"[^a-z]", "", name.lower())
    for cid, c in chars.items():
        names = {cid, c["name"], c["name"].split()[0], c.get("label", ""), c.get("nickname", "")}
        if n in {re.sub(r"[^a-z]", "", x.lower()) for x in names if x}:
            return cid
    return None


def parse_script(path: Path, chars: dict) -> dict:
    """The plain-text script: 'Name: text', 'Name (shocked): text', '[show: URL]', 'hook: ...', '# notes'."""
    lines, hook, problems = [], "", []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        t = raw.strip()
        if not t or t.startswith("#"):
            continue
        m = re.match(r"(?i)^hook\s*:\s*(.+)$", t)
        if m:
            hook = m.group(1).strip()
            continue
        m = re.match(r"^([^:()\[\]]+?)\s*(\((shocked|normal)\))?\s*:\s*(.+)$", t, re.I)
        if not m:
            problems.append(f"line {n}: expected 'Name: text', got {t!r}")
            continue
        cid = find_char(m.group(1), chars)
        if not cid:
            problems.append(f"line {n}: unknown character {m.group(1)!r} (known: {', '.join(c['name'] for c in chars.values())})")
            continue
        text, show, job_ref = m.group(4).strip(), None, None
        s = re.search(r"\[show:\s*(\S+?)\s*\]", text)
        if s:
            show, text = s.group(1), (text[:s.start()] + text[s.end():]).strip()
        j = re.search(r"\[job:\s*(\d+)\s*\]", text)  # a series script's job line: card overlay + the job's own page
        if j:
            job_ref, text = int(j.group(1)), (text[:j.start()] + text[j.end():]).strip()
        lines.append({"speaker": cid, "text": text, "expression": "shocked" if (m.group(3) or "").lower() == "shocked" else "normal",
                      "show": show, "job_ref": job_ref})
    if problems:
        sys.exit("the script has problems:\n  " + "\n  ".join(problems))
    if len({ln["speaker"] for ln in lines}) < 1:
        sys.exit("the script has no lines")
    return {"lines": lines, "hook_card": hook}


def build_custom(script_path: Path, opts: dict, day: str, folder: Path | None = None, jobs: list[dict] | None = None,
                 title: str = "", track: str = "custom") -> Path:
    """Writes script.json for your own script into content/custom/<name>/ (or `folder`) and returns that folder.
    `jobs` (a series episode) gives "[job: N]" lines their job: the card overlay and the job's own page."""
    chars = characters()
    parsed = parse_script(script_path, chars)
    speakers = list(dict.fromkeys(ln["speaker"] for ln in parsed["lines"]))
    left = find_char(opts["left"], chars) if opts.get("left") else None
    right = find_char(opts["right"], chars) if opts.get("right") else None
    order = [x for x in [left, right] if x] + [s for s in speakers if s not in (left, right)]
    if len(order) > 2:
        sys.exit(f"a reel has two characters; the script has {', '.join(order)}")
    cast = []
    for cid, pos in zip(order, ["left", "right"]):
        c = chars[cid]
        cast.append({"id": cid, "position": pos, **{k: v for k, v in c.items() if k != "label"}})
    names = {c["id"]: c["name"] for c in cast}
    name = opts.get("name") or f"{script_path.stem}-{day}"
    folder = folder or ROOT / "content" / "custom" / name
    folder.mkdir(parents=True, exist_ok=True)
    words = sum(len(ln["text"].split()) for ln in parsed["lines"])

    def job_of(ln: dict) -> dict | None:
        n = ln.get("job_ref")
        return (jobs or [])[n - 1] if n and jobs and n <= len(jobs) else None

    script = {
        "date": day, "title": title or name, "track": track, "format": track, "cast": cast,
        "lines": [{**{k: v for k, v in ln.items() if k != "job_ref"}, "name": names[ln["speaker"]],
                   "job_id": (job_of(ln) or {}).get("id"), "job": job_of(ln)} for ln in parsed["lines"]],
        "hook_card": (parsed["hook_card"] or opts.get("hook_card") or "").strip(),
        "caption": "", "hashtags": [], "words": words, "model": "your script",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (folder / "script.json").write_text(json.dumps(script, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (folder / "script.md").write_text(f"# {name}\n\n{words} words · your script\n\n" + "".join(
        f"**{names[ln['speaker']]}**{' (shocked)' if ln['expression'] == 'shocked' else ''}: {ln['text']}  \n" for ln in parsed["lines"]), encoding="utf-8")
    print(f"script: {len(parsed['lines'])} lines, {words} words, {' + '.join(names[c['id']] for c in cast)} -> {folder}")
    return folder


SECTION_TOOL = {
    "content": "content-data-preparation", "script": "script-writer", "voice": "voice-generator",
    "captions": "caption-maker", "visuals": "visual-capture", "compose": "composer", "normalize": "normalize-video",
}


def toml_value(raw: str):
    """'3.6' -> 3.6, 'true' -> True, '["a","b"]' -> list; anything else is a plain string."""
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def parse_sets(items: list[str], run_opts: dict) -> tuple[dict, list[str]]:
    """'compose.panel.scroll=120' style settings, checked against the tool configs so a typo fails the run.
    Returns the [run] changes and the TOML lines for $VG_SET_FILE."""
    run_changes, lines, problems = {}, [], []
    for item in items:
        if "=" not in item:
            problems.append(f"{item!r}: expected key=value")
            continue
        key, raw = (x.strip() for x in item.split("=", 1))
        parts = key.split(".")
        if parts[0] == "run" and len(parts) == 2:
            if parts[1] not in run_opts:
                problems.append(f"{key}: no such run option (known: {', '.join(run_opts)})")
            run_changes[parts[1]] = toml_value(raw)
            continue
        tool = SECTION_TOOL.get(parts[0])
        if not tool or len(parts) < 2:
            problems.append(f"{key}: start with one of {', '.join(['run', *SECTION_TOOL])}, e.g. compose.panel.scroll")
            continue
        node = tomllib.loads((TOOLS / tool / "config.toml").read_text(encoding="utf-8"))
        for i, part in enumerate(parts[1:], 1):
            if not isinstance(node, dict) or part not in node:
                have = ", ".join(node) if isinstance(node, dict) else "(a value, not a table)"
                problems.append(f"{key}: {'.'.join(parts[:i + 1])!r} not in tools/{tool}/config.toml (there: {have})")
                break
            node = node[part]
        else:
            value = toml_value(raw)
            lines.append(f"{'.'.join(json.dumps(p) for p in parts)} = {json.dumps(value) if isinstance(value, str) else raw}")
    if problems:
        sys.exit("settings problems:\n  " + "\n  ".join(problems))
    return run_changes, lines


def script_from_text(text: str, name: str) -> Path:
    """A script pasted in one line ('Riya: ... | Malhotra: ...', the GitHub form has no multi-line box) -> a file."""
    body = "\n".join(x.strip() for x in re.split(r"\s\|\s|\\n", text) if x.strip())
    path = ROOT / "content" / "custom" / f"{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body + "\n", encoding="utf-8")
    return path


# ---- series: a hand-written episode for a date (series/<YYYY-MM-DD>.txt), with that day's fresh jobs filled in ----

SERIES_DIR = ROOT / "series"
SLOT = re.compile(r"\{job(\d)(?:\.(company|title|city|where|posted|url))?\}")
ROMAN = {"I": "One", "II": "Two", "III": "Three"}


def spoken_title(title: str) -> str:
    """A job title as it is said aloud: no brackets, no second part ("QA Engineer I, Music Client QA" -> "QA Engineer One")."""
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", title)
    t = re.split(r"\s+[-–|/]\s+|,\s*", t)[0].strip()
    return re.sub(r"\b(I|II|III)$", lambda m: ROMAN[m.group(1)], t)


def split_sections(text: str) -> dict:
    """'body' plus the parts after '--- description', '--- trial' and '--- trial description' lines."""
    parts, cur = {"body": []}, "body"
    for raw in text.splitlines():
        m = re.match(r"^---\s*(description|trial description|trial)\s*$", raw.strip(), re.I)
        if m:
            cur = m.group(1).lower()
            parts[cur] = []
            continue
        parts.setdefault(cur, []).append(raw)
    return {k: "\n".join(v).strip() for k, v in parts.items()}


def dialogue_lines(body: str) -> list[str]:
    """Spoken lines and 'hook:' of a script body; notes ('# ...') are dropped (they may mention {job2})."""
    return [raw for raw in body.splitlines() if not raw.strip().startswith("#")]


def parse_series(path: Path) -> dict:
    """Header (title, cast, jobs, and the variant lines trial_hook / trial_card / yt_last), the dialogue, the
    description after '--- description', and an optional stand-alone trial version after '--- trial' (with its own
    '--- trial description')."""
    sec = split_sections(path.read_text(encoding="utf-8"))
    head, lines = {}, []
    for raw in dialogue_lines(sec["body"]):
        m = re.match(r"^\s*(title|cast|jobs|funnel|trial_hook|trial_card|yt_last|threads)\s*:\s*(.+)$", raw, re.I)
        if m:
            head[m.group(1).lower()] = m.group(2).split("#")[0].strip() if m.group(1).lower() in ("title", "cast", "jobs", "funnel") else m.group(2).strip()
        else:
            lines.append(raw)
    description = sec.get("description", "")
    niches, _, count = head.get("jobs", "india-freshers, 1").partition(",")
    return {
        "title": head.get("title", path.stem),
        "cast": [c.strip() for c in head.get("cast", "").split(",") if c.strip()],
        "niches": [n.strip() for n in niches.split("|") if n.strip()],
        "count": int(count.strip() or 1),
        "dialogue": "\n".join(lines),
        "description": description.strip(),
        "trial_hook": head.get("trial_hook", ""), "trial_card": head.get("trial_card", ""), "yt_last": head.get("yt_last", ""),
        "threads": head.get("threads", ""),
        "funnel": head.get("funnel", "").lower(),  # top | middle | bottom: the episode's one job in the funnel (2026-11-13)
        "trial_dialogue": "\n".join(dialogue_lines(sec.get("trial", ""))).strip(),
        "trial_description": sec.get("trial description", ""),
    }


def fill_slots(text: str, jobs: list[dict]) -> str:
    def one(m: re.Match) -> str:
        n, field = int(m.group(1)), m.group(2)
        j = jobs[n - 1]
        city = (j.get("where") or "").split(",")[0].strip()
        values = {"company": j["company"], "title": spoken_title(j["title"]), "city": city, "where": j.get("where", ""),
                  "posted": j.get("posted", ""), "url": j.get("apply_url", "")}
        return values[field] if field else f"{j['company']}, {values['title']}, {city}"
    return SLOT.sub(one, text)


def jobs_block(jobs: list[dict]) -> str:
    return "\n".join(f"- {j['title']} at {j['company']} ({j.get('where', '')}, posted {j.get('posted', '')})\n  Apply: {j['apply_url']}" for j in jobs)


def with_platform_link(desc: str, platform: dict) -> str:
    """Adds the day's platform (picked by the brief tool, with its cooldown) and its link above the hashtag line, so the
    description points to n8n / api.market / Zyla ... and not only to the website in the bio (user, 2026-11-04)."""
    if not platform.get("url") or platform["url"] in desc:
        return desc
    name = platform.get("name", platform.get("id", ""))
    line = f"{name[:1].upper() + name[1:]}: {platform['url']}"
    lines = desc.rstrip().splitlines()
    at = len(lines) - 1 if lines and lines[-1].lstrip().startswith("#") else len(lines)
    return "\n".join(lines[:at]).rstrip() + f"\n\n{line}\n\n" + "\n".join(lines[at:])


def is_series_format(path: Path) -> bool:
    """A custom script written like an episode (a 'jobs:' header or a '--- description' part) is made like one."""
    text = path.read_text(encoding="utf-8")
    return bool(re.search(r"(?mi)^\s*jobs\s*:", text) or "\n--- description" in text)


def run_series(path: Path, day: str, opts: dict, env: dict, out: Path | None = None) -> Path | None:
    """Today's episode: fresh jobs from the brief tool (fresher filter, live links, cooldowns), slots filled, script.json
    written to content/<day>. None when no niche gives enough jobs (the caller then runs the full AI pipeline).
    out: a custom script in episode form goes to its own folder, and its jobs are a new entry in today's history
    (no --replace), so the day's real episode keeps its record."""
    ep = parse_series(path)
    chars = characters()
    cast = [find_char(c, chars) for c in ep["cast"]]
    if len(cast) != 2 or not all(cast):
        sys.exit(f"{path.name}: 'cast: LEFT, RIGHT' must name two characters (got {ep['cast']})")
    brief_dir = ROOT / "content" / day  # where the brief tool writes
    folder = out or brief_dir
    folder.mkdir(parents=True, exist_ok=True)
    needed = max([int(n) for n in re.findall(r"\{job(\d)", ep["dialogue"])] + [ep["count"], 0])
    jobs: list[dict] = []
    platform: dict = {}
    # "jobs: ..., 0" and no {job} slot = a pure value episode: no brief, no jobs, no platform link. Only 2-3 reels in
    # 10 promote anything (user, 2026-11-07)
    for niche in ep["niches"] if needed else []:
        print(f"\n== brief (series, niche {niche})", flush=True)
        (brief_dir / "brief.json").unlink(missing_ok=True)  # a failed niche writes none; never read an earlier run's
        r = subprocess.run([sys.executable, "-u", str(STEP["brief"]), "--date", day, "--track", "job_drop", "--niche", niche,
                            "--cast", ",".join(cast), "--jobs", str(needed)] + ([] if out else ["--replace"]), env=env)
        if r.returncode == 0 and (brief_dir / "brief.json").exists():
            brief = json.loads((brief_dir / "brief.json").read_text(encoding="utf-8"))
            jobs, platform = brief.get("jobs", []), brief.get("platform") or {}
            if len(jobs) >= needed:
                break
        print(f"  {niche}: {len(jobs)} job(s), {needed} needed")
    if len(jobs) < needed:
        return None
    if folder != brief_dir and needed:
        shutil.copy2(brief_dir / "brief.json", folder / "brief.json")
    filled = folder / "series-script.txt"
    filled.write_text(fill_slots(ep["dialogue"], jobs), encoding="utf-8")
    build_custom(filled, {**opts, "left": cast[0], "right": cast[1]}, day, folder=folder, jobs=jobs, title=ep["title"], track="series")
    if ep["funnel"]:  # kept with the reel, so results can later be compared per funnel stage
        script = json.loads((folder / "script.json").read_text(encoding="utf-8"))
        script["funnel"] = ep["funnel"]
        (folder / "script.json").write_text(json.dumps(script, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if ep["description"]:
        desc = with_platform_link(fill_slots(ep["description"].replace("{jobs}", jobs_block(jobs[:needed])), jobs), platform)
        (folder / "description.txt").write_text(desc + "\n", encoding="utf-8")
        script = json.loads((folder / "script.json").read_text(encoding="utf-8"))
        script["caption"] = desc  # the same place the AI pipeline keeps its caption
        (folder / "script.json").write_text(json.dumps(script, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # 'threads:' = the whole text of the Threads post: one question people can answer, then the video (user, 2026-11-12).
    # Without it threads-publisher falls back to the description.
    (folder / "threads.txt").unlink(missing_ok=True)
    if ep["threads"]:
        (folder / "threads.txt").write_text(fill_slots(ep["threads"], jobs) + "\n", encoding="utf-8")
    # the variants the episode asks for (made after the reel by make_variants)
    spec: dict = {}
    fill = lambda t: fill_slots(t, jobs) if t else ""  # noqa: E731
    if ep["trial_dialogue"]:
        tdesc = ep["trial_description"]
        spec["trial"] = {"kind": "standalone", "dialogue": fill(ep["trial_dialogue"]),
                         "description": with_platform_link(fill(tdesc.replace("{jobs}", jobs_block(jobs[:needed]))), platform) if tdesc else ""}
    elif ep["trial_hook"]:
        spec["trial"] = {"kind": "hook", "first_line": fill(ep["trial_hook"]), "hook_card": ep["trial_card"]}
    if ep["yt_last"]:
        spec["youtube"] = {"last_line": fill(ep["yt_last"])}
    (folder / "variants.json").unlink(missing_ok=True)
    if spec:
        (folder / "variants.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"series: {path.name} '{ep['title']}' with {needed} job(s)" + (f": {', '.join(j['company'] for j in jobs[:needed])}" if needed else " (value episode)"))
    return folder


# ---- variants: extra versions of the finished reel, in <folder>/variants/<kind>/ (user, 2026-11-08) ----
# trial    an Instagram trial reel (shown only to non-followers): the same reel with a second opening line and hook card,
#          or a stand-alone version written for people who never saw the series. From the episode's trial_hook /
#          trial_card or "--- trial" part, or an AI script's alt_hook.
# youtube  the reel saying "subscribe" where it says "follow" (or the episode's yt_last as the last line).
# Each one is a whole new voice + captions + render on the reel's own background stretch. A failed variant is only a
# warning: the reel itself is already made.

def for_youtube(lines: list[dict], last_line: str = "") -> list[dict] | None:
    """The lines with the call to follow turned into one to subscribe (only in the last two lines, so a story line
    like "follow-up mail" stays), or None when nothing changes."""
    out = [dict(ln) for ln in lines]
    if last_line:
        out[-1]["text"] = last_line
    for ln in out[-2:]:
        ln["text"] = re.sub(r"\b([Ff])ollow\b(?!-)", lambda m: "Subscribe" if m.group(1) == "F" else "subscribe", ln["text"])
    return out if [ln["text"] for ln in out] != [ln["text"] for ln in lines] else None


def make_variants(folder: Path, opts: dict, day: str, env: dict) -> list[str]:
    if not opts.get("variants", True):
        return []
    script = json.loads((folder / "script.json").read_text(encoding="utf-8"))
    vfile = folder / "variants.json"
    spec = json.loads(vfile.read_text(encoding="utf-8")) if vfile.is_file() else {}
    alt = script.get("alt_hook") or {}
    if "trial" not in spec and alt.get("text"):
        spec["trial"] = {"kind": "hook", "first_line": alt["text"], "hook_card": alt.get("hook_card", "")}
    yt_lines = for_youtube(script["lines"], (spec.get("youtube") or {}).get("last_line", ""))
    spec.pop("youtube", None)
    if yt_lines:
        spec["youtube"] = {"kind": "lines", "lines": yt_lines}
    if not spec:
        return []
    comp = json.loads((folder / "compose.json").read_text(encoding="utf-8"))
    background = f"{comp['background']['clip']}@{comp['background']['offset']}"
    main_desc = (folder / "description.txt").read_text(encoding="utf-8") if (folder / "description.txt").is_file() else ""
    made = []
    for kind, v in spec.items():
        vf = folder / "variants" / kind
        shutil.rmtree(vf, ignore_errors=True)
        vf.mkdir(parents=True)
        print(f"\n==== variant: {kind} ({v.get('kind')})", flush=True)
        try:
            if v.get("kind") == "standalone":
                src = vf / "variant-script.txt"
                src.write_text(v["dialogue"] + "\n", encoding="utf-8")
                brief = json.loads((folder / "brief.json").read_text(encoding="utf-8")) if (folder / "brief.json").is_file() else {}
                cast = {c["position"]: c["id"] for c in script["cast"]}
                build_custom(src, {**opts, "left": cast.get("left"), "right": cast.get("right")}, day, folder=vf,
                             jobs=brief.get("jobs") or [], title=script.get("title", ""), track=script.get("track", "custom"))
                if (folder / "brief.json").is_file():
                    shutil.copy2(folder / "brief.json", vf / "brief.json")
            else:
                s = json.loads(json.dumps(script))
                if v.get("kind") == "hook":
                    s["lines"][0]["text"] = v["first_line"]
                    if v.get("hook_card"):
                        s["hook_card"] = v["hook_card"]
                else:
                    s["lines"] = v["lines"]
                (vf / "script.json").write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                if (folder / "visuals.json").is_file():  # same lines in the same places: the reel's own screenshots
                    shutil.copy2(folder / "visuals.json", vf / "visuals.json")
                    if (folder / "visuals").is_dir():
                        shutil.copytree(folder / "visuals", vf / "visuals")
            desc = v.get("description") or main_desc
            if kind == "youtube":
                desc = re.sub(r"\b([Ff])ollow karo\b", lambda m: ("S" if m.group(1) == "F" else "s") + "ubscribe karo", desc)
            if desc:
                (vf / "description.txt").write_text(desc, encoding="utf-8")
            target = ["--script", str(vf / "script.json")]
            run("voice", target, env)
            run("captions", target, env)
            if v.get("kind") == "standalone" and opts.get("visuals", True):
                run("visuals", ["--folder", str(vf)], env)
            run("compose", ["--folder", str(vf), "--background", background], env)
            made.append(kind)
            print(f"variant {kind}: {vf / 'reel.mp4'}")
        except SystemExit as e:
            print(f"WARNING: variant {kind} not made ({e}); the reel itself is fine", flush=True)
    return made


def main() -> None:
    p = argparse.ArgumentParser(description="Make a reel: python run.py ats | custom [SCRIPT.txt]")
    p.add_argument("mode", help="ats, custom, or a mode .toml file")
    p.add_argument("script", nargs="?", help="custom: your script file (overrides script_file)")
    p.add_argument("--script-text", help="your script in one line, lines split by ' | ' (instead of a file)")
    p.add_argument("--left"); p.add_argument("--right"); p.add_argument("--hook")
    p.add_argument("--visuals", choices=["on", "off"]); p.add_argument("--clip"); p.add_argument("--name")
    p.add_argument("--track", help="AI script: job_drop or product_pitch"); p.add_argument("--niche", help="AI script: a niche id")
    p.add_argument("--format", help="AI script: a format id"); p.add_argument("--platform", help="AI script: a platform id")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="change any setting for this run: compose.panel.scroll=120, voice.voice.target_wps=3.6 (repeatable; "
                        "several in one go split by ';')")
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--no-series", action="store_true", help="ignore series/<date>.txt and run the AI pipeline")
    a = p.parse_args()

    mode_file = Path(a.mode) if a.mode.endswith(".toml") else ROOT / "modes" / f"{a.mode}.toml"
    if not mode_file.is_file():
        sys.exit(f"no mode file {mode_file}")
    mode_file = mode_file.resolve()
    opts = tomllib.loads(mode_file.read_text(encoding="utf-8")).get("run", {})
    sets = [s for item in a.set for s in item.split(";") if s.strip()]
    run_changes, set_lines = parse_sets(sets, opts)
    opts.update(run_changes)
    for k in ("left", "right", "clip", "name", "track", "niche", "format", "platform"):
        if getattr(a, k):
            opts[k] = getattr(a, k)
    if a.hook:
        opts["hook_card"] = a.hook
    if a.visuals:
        opts["visuals"] = a.visuals == "on"
    if a.script_text and a.script_text.strip():
        script_file = script_from_text(a.script_text, opts.get("name") or f"script-{a.date}")
    else:
        script_file = Path(a.script).resolve() if a.script else ((mode_file.parent / opts["script_file"]).resolve() if opts.get("script_file") else None)
    os.environ["VG_MODE_FILE"] = str(mode_file)  # also for the character lookups done here
    set_file = ROOT / ".run-settings.toml"  # paths in --set values are relative to the repo root
    set_file.write_text("# written by run.py for this run (--set); safe to delete\n" + "\n".join(set_lines) + "\n", encoding="utf-8")
    os.environ["VG_SET_FILE"] = str(set_file)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    print(f"mode: {mode_file.name}" + (f", script: {script_file.name}" if script_file else ", series episode or AI script") + f", visuals {'on' if opts.get('visuals', True) else 'off'}")
    for line in set_lines:
        print(f"  setting {line}")

    series_file = SERIES_DIR / f"{a.date}.txt"
    series_folder = None
    if not script_file and series_file.is_file() and not a.no_series:
        print(f"series episode for {a.date}: {series_file.name}")
        series_folder = run_series(series_file, a.date, opts, env)
        if not series_folder:
            print("not enough fresh jobs for the episode's slots: making today's reel with the AI pipeline instead")
    if series_folder:
        folder = series_folder
        target = ["--script", str(folder / "script.json")]
    elif script_file:
        if not script_file.is_file():
            sys.exit(f"script not found: {script_file}")
        if is_series_format(script_file):
            # fresh jobs in the slots, job cards, description with apply + platform links, like a series episode
            folder = run_series(script_file, a.date, opts, env, out=ROOT / "content" / "custom" / (opts.get("name") or f"{script_file.stem}-{a.date}"))
            if not folder:
                sys.exit("not enough fresh jobs for the script's job slots")
        else:
            folder = build_custom(script_file, opts, a.date)
        target = ["--script", str(folder / "script.json")]
    else:
        brief_args = ["--date", a.date]
        for k in ("track", "niche", "format", "platform"):
            if opts.get(k):
                brief_args += [f"--{k}", str(opts[k])]
        if opts.get("left") or opts.get("right"):
            if not (opts.get("left") and opts.get("right")):
                sys.exit("give both --left and --right to pick the characters")
            chars = characters()
            l, r = find_char(opts["left"], chars), find_char(opts["right"], chars)
            if not l or not r:
                sys.exit(f"unknown character in {opts['left']!r} / {opts['right']!r}")
            brief_args += ["--cast", f"{l},{r}"]
        run("brief", brief_args, env)
        run("script", ["--date", a.date], env)
        folder = ROOT / "content" / a.date
        target = ["--script", str(folder / "script.json")]

    run("voice", target, env)
    run("captions", target, env)
    if opts.get("visuals", True):
        run("visuals", ["--folder", str(folder)], env)
    else:
        (folder / "visuals.json").unlink(missing_ok=True)  # the composer shows whatever visuals.json lists
    run("compose", ["--folder", str(folder)] + (["--clip", opts["clip"]] if opts.get("clip") else []), env)
    print(f"\nreel: {folder / 'reel.mp4'}")
    made = make_variants(folder, opts, a.date, env)
    if made:
        print(f"variants: {', '.join(made)} (in {folder / 'variants'})")


if __name__ == "__main__":
    main()
