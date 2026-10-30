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

Settings layer up: each tool's own config.toml < config.toml (master) < the mode file. This script only runs the
tools in order with the mode file in $VG_MODE_FILE; every tool still works on its own.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
        text, show = m.group(4).strip(), None
        s = re.search(r"\[show:\s*(\S+?)\s*\]", text)
        if s:
            show, text = s.group(1), (text[:s.start()] + text[s.end():]).strip()
        lines.append({"speaker": cid, "text": text, "expression": "shocked" if (m.group(3) or "").lower() == "shocked" else "normal", "show": show})
    if problems:
        sys.exit("the script has problems:\n  " + "\n  ".join(problems))
    if len({ln["speaker"] for ln in lines}) < 1:
        sys.exit("the script has no lines")
    return {"lines": lines, "hook_card": hook}


def build_custom(script_path: Path, opts: dict, day: str) -> Path:
    """Writes script.json for your own script into content/custom/<name>/ and returns that folder."""
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
    folder = ROOT / "content" / "custom" / name
    folder.mkdir(parents=True, exist_ok=True)
    words = sum(len(ln["text"].split()) for ln in parsed["lines"])
    script = {
        "date": day, "title": name, "track": "custom", "format": "custom", "cast": cast,
        "lines": [{**ln, "name": names[ln["speaker"]], "job_id": None, "job": None} for ln in parsed["lines"]],
        "hook_card": (parsed["hook_card"] or opts.get("hook_card") or "").strip(),
        "caption": "", "hashtags": [], "words": words, "model": "your script",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (folder / "script.json").write_text(json.dumps(script, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (folder / "script.md").write_text(f"# {name}\n\n{words} words · your script\n\n" + "".join(
        f"**{names[ln['speaker']]}**{' (shocked)' if ln['expression'] == 'shocked' else ''}: {ln['text']}  \n" for ln in parsed["lines"]), encoding="utf-8")
    print(f"script: {len(parsed['lines'])} lines, {words} words, {' + '.join(names[c['id']] for c in cast)} -> {folder}")
    return folder


def main() -> None:
    p = argparse.ArgumentParser(description="Make a reel: python run.py ats | custom [SCRIPT.txt]")
    p.add_argument("mode", help="ats, custom, or a mode .toml file")
    p.add_argument("script", nargs="?", help="custom: your script file (overrides script_file)")
    p.add_argument("--left"); p.add_argument("--right"); p.add_argument("--hook")
    p.add_argument("--visuals", choices=["on", "off"]); p.add_argument("--clip"); p.add_argument("--name")
    p.add_argument("--date", default=date.today().isoformat())
    a = p.parse_args()

    mode_file = Path(a.mode) if a.mode.endswith(".toml") else ROOT / "modes" / f"{a.mode}.toml"
    if not mode_file.is_file():
        sys.exit(f"no mode file {mode_file}")
    mode_file = mode_file.resolve()
    opts = tomllib.loads(mode_file.read_text(encoding="utf-8")).get("run", {})
    for k in ("left", "right", "clip", "name"):
        if getattr(a, k):
            opts[k] = getattr(a, k)
    if a.hook:
        opts["hook_card"] = a.hook
    if a.visuals:
        opts["visuals"] = a.visuals == "on"
    script_file = Path(a.script).resolve() if a.script else ((mode_file.parent / opts["script_file"]).resolve() if opts.get("script_file") else None)
    os.environ["VG_MODE_FILE"] = str(mode_file)  # also for the character lookups done here
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    print(f"mode: {mode_file.name}" + (f", script: {script_file.name}" if script_file else ", AI script") + f", visuals {'on' if opts.get('visuals', True) else 'off'}")

    if script_file:
        if not script_file.is_file():
            sys.exit(f"script not found: {script_file}")
        folder = build_custom(script_file, opts, a.date)
        target = ["--script", str(folder / "script.json")]
    else:
        brief_args = ["--date", a.date]
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


if __name__ == "__main__":
    main()
