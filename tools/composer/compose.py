"""
composer: puts the day's reel together. See README.md.

    python tools/composer/compose.py [--date YYYY-MM-DD | --folder DIR] [--config FILE] [--reroll N]

Reads from the day's folder: script.json, voice.wav, timeline.json, captions.ass. Writes reel.mp4 and compose.json.
  - background: a random stretch of a normalised clip (seeded by the date, so a rerun gives the same reel; --reroll
    picks another), slightly dimmed
  - characters: only the one speaking is on screen. It slides in from its own side when its turn starts and slides
    out as the other one slides in from the other side. Its image follows the line's expression (talk / shock)
  - voice and captions on top
Character images are trimmed to the character (one box per pack, so expressions don't shift), scaled, and mirrored when
the character would otherwise face away from the middle.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import os
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"
SECTION = "compose"  # this tool's table in the master config and the mode files

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



def tool(name: str, folder: str) -> str:
    path = str(Path(folder) / name) if folder else shutil.which(name)
    if not path or not (shutil.which(path) or Path(path).exists() or Path(path + ".exe").exists()):
        sys.exit(f"{name} not found. Install FFmpeg (Windows: winget install --id Gyan.FFmpeg -e) and open a new terminal, or set paths.ffmpeg_dir.")
    return path


def probe_seconds(ffprobe: str, f: Path) -> float:
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)], capture_output=True, text=True)
    return float(r.stdout.strip())


# ---- background ----

def pick_background(folder: Path, ffprobe: str, need: float, margin: float, rng: random.Random, only: str | None,
                    ffmpeg: str = "", bg_cfg: dict | None = None, log=print) -> tuple[Path, float]:
    bg_cfg = bg_cfg or {}
    known = {}
    manifest = folder / "manifest.json"
    if manifest.exists():
        known = {c["file"]: c["duration"] for c in json.loads(manifest.read_text(encoding="utf-8")).get("clips", [])}
    clips = sorted(p for p in folder.glob("*.mp4") if not p.name.endswith(".part.mp4") and (not only or p.name == only))
    if only and not clips:
        sys.exit(f"{only} not found in {folder}")
    usable = []
    for c in clips:
        length = known.get(c.name) or probe_seconds(ffprobe, c)
        if length >= need + 2 * margin:
            usable.append((c, length))
    if not usable:
        sys.exit(f"no background clip in {folder} is longer than {need + 2 * margin:.0f} s; run normalize-video first")
    picks = []
    for _ in range(max(1, bg_cfg.get("candidates", 1))):
        clip, length = rng.choice(usable)
        picks.append((clip, round(rng.uniform(margin, length - need - margin), 2)))
    if len(picks) == 1 or not ffmpeg:
        return picks[0]
    # 2026-10-30: one stretch was a still white wall, flat and bright behind the captions. Prefer movement, not glare.
    scored = []
    for clip, off in picks:
        motion, bright = stretch_stats(ffmpeg, clip, off, need)
        score = motion - max(0.0, bright - bg_cfg.get("max_brightness", 150)) * bg_cfg.get("glare_penalty", 1.0)
        scored.append((score, clip, off, motion, bright))
        log(f"  background {clip.name} @{off:.0f}s: motion {motion:.1f}, brightness {bright:.0f}")
    best = max(scored, key=lambda s: s[0])
    return best[1], best[2]


def stretch_stats(ffmpeg: str, clip: Path, offset: float, length: float) -> tuple[float, float]:
    """(mean frame-to-frame change, mean brightness 0-255) of a stretch, from tiny grey frames at 2 fps."""
    import numpy as np
    w, h = 54, 96
    r = subprocess.run([ffmpeg, "-v", "error", "-ss", str(offset), "-t", str(length), "-i", str(clip),
                        "-vf", f"fps=2,scale={w}:{h},format=gray", "-f", "rawvideo", "-"], capture_output=True)
    f = np.frombuffer(r.stdout, np.uint8)
    f = f[: len(f) // (w * h) * w * h].reshape(-1, h, w).astype(np.float32)
    if len(f) < 2:
        return 0.0, 255.0
    return float(np.abs(np.diff(f, axis=0)).mean()), float(f.mean())


# ---- characters ----

def prepare_pack(pack_dir: Path, names: set[str], height: int, flip: bool, no_flip: list[str], out: Path, body: str) -> dict:
    """Trims every image of a pack to one shared box (bottom-aligned, so switching expression doesn't move the
    character), scales it to `height` and mirrors it if needed. Returns name -> (file, width, height), plus "_body":
    the (left, right) x of the `body` image's figure inside that box, so placement follows the figure, not a waving
    arm (sharma-ji's talk.png gesture made the box 950 px wide)."""
    imgs = {n: Image.open(pack_dir / f"{n}.png").convert("RGBA") for n in names | {body}}
    boxes = {n: im.getchannel("A").point(lambda a: 255 if a > 16 else 0).getbbox() for n, im in imgs.items()}
    left, right, up = 10**9, 0, 0  # x range, and height measured up from the bottom edge
    for n, im in imgs.items():
        box = boxes[n]
        if not box:
            continue
        left, right = min(left, box[0]), max(right, box[2])
        up = max(up, im.height - box[1])
    scale = height / up
    w_all = round((right - left) * scale)
    bl, br = (boxes[body][0] - left) * scale, (boxes[body][2] - left) * scale
    done: dict = {"_body": (w_all - br, w_all - bl) if flip else (bl, br)}
    for n, im in imgs.items():
        canvas = Image.new("RGBA", (right - left, up))
        canvas.alpha_composite(im, dest=(0, max(0, up - im.height)), source=(left, max(0, im.height - up)))
        w = w_all
        canvas = canvas.resize((w, height), Image.LANCZOS)
        if flip and n not in no_flip:
            canvas = canvas.transpose(Image.FLIP_LEFT_RIGHT)
        f = out / f"{pack_dir.name}-{n}.png"
        canvas.save(f)
        done[n] = (f, w, height)
    return done


def plan_turns(timeline: dict, total: float, cc: dict) -> list[dict]:
    """Consecutive lines by the same speaker form one turn: on screen from its slide-in until the next turn's
    slide-in has finished."""
    turns: list[dict] = []
    for ln in timeline["lines"]:
        if turns and turns[-1]["speaker"] == ln["speaker"]:
            turns[-1]["lines"].append(ln)
        else:
            turns.append({"speaker": ln["speaker"], "lines": [ln]})
    for i, t in enumerate(turns):
        t["in"] = max(0.0, t["lines"][0]["start"] - cc["lead"])
    if turns and cc.get("first_in_place", True):
        turns[0]["in"] = -cc["slide"]  # already on screen at frame 0: an empty first frame loses the viewer (virality review)
    for i, t in enumerate(turns):
        t["out"] = turns[i + 1]["in"] if i + 1 < len(turns) else None
        t["end"] = min(total, t["out"] + cc["slide"]) if t["out"] is not None else total
    return turns


def x_expr(side: str, w: int, W: int, target: float, t_in: float, t_out: float | None, slide: float) -> str:
    """Eases in from off-screen on its own side (cubic ease-out) to `target` and back out (quadratic ease-in)."""
    target, off = (round(target), -w) if side == "left" else (round(target), W)
    e_in = f"(1-pow(1-min(1,max(0,(t-{t_in:.3f})/{slide})),3))"
    x = f"{off}+({target - off})*{e_in}"
    if t_out is not None:
        x += f"+({off - target})*pow(min(1,max(0,(t-{t_out:.3f})/{slide})),2)"
    return x


def build_overlays(turns: list[dict], cast: dict, images: dict, cfg: dict) -> list[dict]:
    """One overlay per image use: {file, x, y, enable}."""
    v, cc = cfg["video"], cfg["characters"]
    out = []
    for t in turns:
        c = cast[t["speaker"]]
        imgs = images[t["speaker"]]
        w = imgs[cc["normal"]][1]
        # the figure (not a waving arm) stops at inner_edge of the width: the left one ends there, the right one
        # starts at 1 - inner_edge (user, 2026-10-30: they came in to the middle; keep them to ~30-40% / ~60-70%).
        # What sticks out on the outer side is cut by the frame edge.
        bl, br = imgs["_body"]
        inner = cc["inner_edge"] * v["width"]
        target = inner - br if c["position"] == "left" else v["width"] - inner - bl
        x = x_expr(c["position"], w, v["width"], target, t["in"], t["out"], cc["slide"])
        y = v["height"] - imgs[cc["normal"]][2] - cc["margin_bottom"]
        for k, ln in enumerate(t["lines"]):
            a = t["in"] if k == 0 else ln["start"]
            b = t["lines"][k + 1]["start"] if k + 1 < len(t["lines"]) else t["end"]
            if ln["expression"] == "shocked":
                # a pack can map an expression to another image ("images = { shocked = "talk" }")
                name = cfg["packs"].get(c["pack"], {}).get("images", {}).get("shocked", cc["shocked"])
                out.append({"file": imgs[name][0], "x": x, "y": y, "enable": f"between(t,{a:.3f},{b:.3f})"})
            else:
                out.append({"file": imgs[cc["normal"]][0], "x": x, "y": y, "enable": f"between(t,{a:.3f},{b:.3f})"})
    return out


# ---- on-screen layer: hook card, job cards, stamps (2026-10-30 review: "show, never just say") ----

def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_text(s: str) -> str:
    return str(s).replace("\\", "").replace("{", "(").replace("}", ")")


def make_overlay(script: dict, timeline: dict, cfg: dict) -> tuple[str, list[tuple[str, float]]]:
    """The ASS file for the on-screen layer, and the (sfx name, time) cues that go with it."""
    o, v = cfg["overlay"], cfg["video"]
    cx = v["width"] // 2
    styles = [
        f"Style: hook,{o['font']},{o['hook_size']},{ass_color('#FFFFFF')},{ass_color('#FFFFFF')},{ass_color('#000000')},{ass_color('#000000', 0x60)},"
        f"-1,0,0,0,100,100,0,0,3,18,0,8,60,60,{o['top']},1",
        f"Style: card,{o['font']},{o['card_size']},{ass_color('#111111')},{ass_color('#111111')},{ass_color('#FFFFFF')},{ass_color('#000000', 0x80)},"
        f"-1,0,0,0,100,100,0,0,3,22,0,8,90,90,{o['top']},1",
        f"Style: stamp,{o['font']},{o['stamp_size']},{ass_color(o['stamp_color'])},{ass_color(o['stamp_color'])},{ass_color('#FFFFFF')},{ass_color('#000000', 0x80)},"
        f"-1,0,0,0,100,100,4,0,1,7,0,5,0,0,0,1",
    ]
    events, cues = [], []
    lines = timeline["lines"]
    # hook card: words land one by one in the first ~1.4 s and stay until the hook's line ends (at least hook_hold)
    card = [w for w in (script.get("hook_card") or "").upper().split() if w]
    if card and lines:
        end = max(o["hook_hold"], lines[0]["end"])
        step = o["hook_build"] / max(1, len(card))
        lands = [k * step for k in range(len(card))]
        # "0 CALLS." / "ZERO REPLIES": from the first zero after word 1 on, the words slam in red with a stamp sound
        zero = next((k for k in range(1, len(card)) if re.sub(r"[^A-Z0-9]", "", card[k]) in ("0", "ZERO", "NO")), None)
        num = re.fullmatch(r"(\d{2,5})([^\d]*)", card[0])
        red = ass_color(o.get("stamp_color", "#E53935"))

        def card_text(first: str, since: float) -> str:
            """The card as seen from `since` on: words not landed yet fade in at their time (relative to since)."""
            parts = []
            for k, word in enumerate(card):
                w = first if k == 0 else ass_text(word)
                t0 = int((lands[k] - since) * 1000)
                style = ""
                if zero is not None and k >= zero:
                    style = f"\\c{red}" + (f"\\fscx190\\fscy190\\t({t0},{t0 + 140},\\fscx100\\fscy100)" if t0 > 0 else "")
                show = f"\\alpha&HFF&\\t({t0},{t0 + 60},\\alpha&H00&)" if k and t0 > 0 else ""
                parts.append(f"{{{show}{style}}}{w}{{\\c&HFFFFFF&}}" if (show or style) else w)
            return " ".join(parts)
        if num:
            # the number races up from 0 while the other words land (virality: "a number races and locks" in 0-3 s)
            n, tail = int(num.group(1)), ass_text(num.group(2))
            steps = max(2, int(o.get("count_steps", 12)))
            dur = o.get("count_seconds", 1.0)
            for s in range(steps):
                a, b = dur * s / steps, dur * (s + 1) / steps
                events.append(f"Dialogue: 2,{ass_time(a)},{ass_time(b)},hook,,0,0,0,,{card_text(f'{round(n * (s + 1) / steps)}{tail}', a)}")
            events.append(f"Dialogue: 2,{ass_time(dur)},{ass_time(end)},hook,,0,0,0,,{{\\fad(0,150)}}{card_text(ass_text(card[0]), dur)}")
        else:
            events.append(f"Dialogue: 2,{ass_time(0)},{ass_time(end)},hook,,0,0,0,,{{\\fad(0,150)}}{card_text(ass_text(card[0]), 0)}")
        if zero is not None:
            cues.append(("stamp", lands[zero]))
    # job cards: while a line about a job is spoken
    by_text = {ln["text"]: ln for ln in script["lines"]}
    for ln in lines:
        job = (by_text.get(ln["text"]) or {}).get("job")
        if not job:
            continue
        title = job["title"] if len(job["title"]) <= o["card_title_max"] else job["title"][: o["card_title_max"] - 1].rstrip(" ,-") + "…"
        where = ", ".join(x for x in [job.get("where", "").split(",")[0], f"posted {job['posted']}" if job.get("posted") else ""] if x)
        text = (f"{{\\move({cx},{o['top'] - 60},{cx},{o['top']},0,180)\\fad(120,150)}}{ass_text(job['company']).upper()}"
                f"\\N{{\\fs{int(o['card_size'] * 0.66)}\\b0}}{ass_text(title)}\\N{{\\fs{int(o['card_size'] * 0.5)}}}{ass_text(where)}")
        events.append(f"Dialogue: 1,{ass_time(ln['start'])},{ass_time(ln['end'] + 0.3)},card,,0,0,0,,{text}")
        cues.append(("pop", ln["start"]))
    # stamps: a word in the dialogue slams a big rotated stamp onto the middle of the frame
    for word, label in o.get("stamps", {}).items():
        for ln in lines:
            for w in ln["words"]:
                if re.sub(r"[^a-z]", "", w["word"].lower()) == word:
                    t = w["start"]
                    text = (f"{{\\pos({cx},{o['stamp_y']})\\frz{o['stamp_angle']}\\fscx220\\fscy220\\t(0,140,\\fscx100\\fscy100)\\fad(0,200)}}"
                            f"{ass_text(label)}")
                    events.append(f"Dialogue: 3,{ass_time(t)},{ass_time(t + o['stamp_hold'])},stamp,,0,0,0,,{text}")
                    cues.append(("stamp", t))
    head = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {v['width']}", f"PlayResY: {v['height']}", "WrapStyle: 0",
            "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
            "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            *styles, "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    return "\n".join(head + events + [""]), cues


def sound_inputs(cfg: dict, base: Path, turns: list[dict], cues: list[tuple[str, float]], total: float, log) -> list[tuple[Path, float, float]]:
    """(file, start time, gain dB) for every sound effect and the music bed whose file exists. Missing files are skipped."""
    a = cfg.get("audio", {})
    sfx_dir = (base / a.get("sfx_dir", "")).resolve()
    out, missing = [], set()

    def add(name: str, t: float):
        f = sfx_dir / a.get("sfx", {}).get(name, "")
        if a.get("sfx", {}).get(name) and f.is_file():
            out.append((f, max(0.0, t), a.get("gain", {}).get(name, a.get("sfx_gain_db", -6))))
        else:
            missing.add(name)
    for t in turns[1:]:
        add("whoosh", t["in"])
    for name, t in cues:
        add(name, t)
    last = -1e9
    shocks = 0
    for t in turns:
        for ln in t["lines"]:
            if ln.get("expression") == "shocked" and shocks < a.get("shock_max", 2) and ln["start"] - last >= a.get("shock_gap", 8):
                add("shock", ln["start"])
                last, shocks = ln["start"], shocks + 1
    music_dir = (base / a.get("music_dir", "")).resolve()
    tracks = sorted(p for p in music_dir.glob("*") if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg")) if music_dir.is_dir() else []
    if tracks:
        out.append((tracks[int(total * 1000) % len(tracks)], -1.0, a.get("music_gain_db", -22)))  # -1: music bed, looped
    if missing:
        log(f"  no sound file for: {', '.join(sorted(missing))} (put them in {sfx_dir})")
    return out


# ---- render ----

def plan_panels(folder: Path, timeline: dict, total: float, cfg: dict) -> list[dict]:
    """visual-capture's screenshots as {file, start, end}: shown while their line is spoken, held up to `hold` s into
    a following line that has no visual of its own."""
    p = cfg.get("panel", {})
    f = folder / "visuals.json"
    if not p.get("enabled", True) or not f.exists():
        return []
    items = {it["line"]: it for it in json.loads(f.read_text(encoding="utf-8")).get("items", []) if (folder / it["file"]).exists()}
    lines = timeline["lines"]
    out = []
    for i, it in sorted(items.items()):
        if i >= len(lines):
            continue
        start, end = lines[i]["start"], lines[i]["end"]
        if i + 1 < len(lines) and i + 1 not in items:
            end = min(lines[i + 1]["end"], end + p.get("hold", 1.5))
        elif i + 1 < len(lines):
            end = lines[i + 1]["start"]
        out.append({"file": folder / it["file"], "start": 0.0 if i == 0 else start, "end": min(end, total),
                    **({"scroll": it["scroll"]} if it.get("scroll") else {})})
    return out


def panel_filter(k: int, pnl: dict, cfg: dict) -> str:
    """Scale the screenshot to the panel width, show a window of it that scrolls down (like a screen recording),
    frame it, and fade it in and out."""
    p = cfg["panel"]
    w, h = p["width"], p["height"]
    a, e = pnl["start"], pnl["end"]
    speed = pnl.get("scroll", p["scroll"])
    scroll = f"[{k}:v]scale={w}:-2,crop={w}:{h}:0:'min(ih-{h},max(0,(t-{a:.3f})*{speed}))',format=rgba,"
    # no fade-in on a panel that starts the reel: frame 0 must already show it
    fade = (f"fade=t=in:st={a:.3f}:d=0.2:alpha=1," if a > 0.05 else "") + f"fade=t=out:st={max(a, e - 0.2):.3f}:d=0.2:alpha=1"
    b = p["border"]
    return scroll + f"pad={w + 2 * b}:{h + 2 * b}:{b}:{b}:color={p['border_color']}," + fade


def render(ffmpeg: str, folder: Path, clip: Path, offset: float, total: float, overlays: list[dict], cfg: dict, fonts_dir: str, log,
           sounds: list[tuple[Path, float, float]] = (), overlay_ass: str | None = None, panels: list[dict] = ()) -> Path:
    v, bg = cfg["video"], cfg["background"]
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{offset}", "-t", f"{total:.3f}", "-i", str(clip),
           "-i", str(folder / "voice.wav")]
    for o in overlays:
        cmd += ["-loop", "1", "-framerate", str(v["fps"]), "-t", f"{total:.3f}", "-i", str(o["file"])]
    for pnl in panels:
        cmd += ["-loop", "1", "-framerate", str(v["fps"]), "-t", f"{total:.3f}", "-i", str(pnl["file"])]
    for f, t, _ in sounds:
        cmd += (["-stream_loop", "-1"] if t < 0 else []) + ["-i", str(f)]
    chain = [f"[0:v]setpts=PTS-STARTPTS,fps={v['fps']},scale={v['width']}:{v['height']},"
             f"eq=brightness={bg['brightness']}:saturation={bg['saturation']}[v0]"]
    for i, o in enumerate(overlays):
        chain.append(f"[v{i}][{i + 2}:v]overlay=x='{o['x']}':y={o['y']}:enable='{o['enable']}':eof_action=pass[v{i + 1}]")
    last = f"v{len(overlays)}"
    p = cfg["panel"] if panels else {}
    pw = p.get("width", 0) + 2 * p.get("border", 0)
    for j, pnl in enumerate(panels):
        k = 2 + len(overlays) + j
        chain.append(panel_filter(k, pnl, cfg) + f"[pn{j}]")
        chain.append(f"[{last}][pn{j}]overlay=x={(v['width'] - pw) // 2}:y={p['top']}:"
                     f"enable='between(t,{pnl['start']:.3f},{pnl['end']:.3f})':eof_action=pass[pv{j}]")
        last = f"pv{j}"
    # relative to the reel's folder (ffmpeg runs there): a Windows drive colon breaks the filter syntax even escaped
    fontsdir = (":fontsdir=" + Path(os.path.relpath(fonts_dir, folder)).as_posix()) if fonts_dir else ""
    subs = f"subtitles=captions.ass{fontsdir}" + (f",subtitles={overlay_ass}{fontsdir}" if overlay_ass else "")
    chain.append(f"[{last}]{subs},format=yuv420p[vout]")
    chain.append(f"[1:a]apad,atrim=0:{total:.3f},aresample=48000[voice]")
    mix = ["[voice]"]
    for j, (f, t, gain) in enumerate(sounds):
        k = 2 + len(overlays) + len(panels) + j
        delay = "" if t < 0 else f"adelay={int(t * 1000)}:all=1,"
        chain.append(f"[{k}:a]{delay}volume={gain}dB,aresample=48000,apad,atrim=0:{total:.3f}[s{j}]")
        mix.append(f"[s{j}]")
    if sounds:
        chain.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first[aout]")
    else:
        chain.append("[voice]anull[aout]")
    part = folder / "reel.part.mp4"
    cmd += ["-filter_complex", ";".join(chain), "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", v["preset"], "-crf", str(v["crf"]), "-r", str(v["fps"]),
            "-c:a", "aac", "-b:a", v["audio_bitrate"], "-ac", "2", "-movflags", "+faststart", str(part)]
    t = time.time()
    r = subprocess.run(cmd, cwd=folder, capture_output=True, text=True)  # cwd: the subtitles filter gets a bare file name
    if r.returncode != 0:
        part.unlink(missing_ok=True)
        sys.exit(f"ffmpeg failed: {r.stderr.strip()[-1500:]}")
    out = folder / "reel.mp4"
    part.replace(out)
    log(f"rendered in {time.time() - t:.0f} s")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Put the day's reel together.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--date", help="YYYY-MM-DD (default: the latest day with captions)")
    p.add_argument("--folder", type=Path, help="a day's folder anywhere")
    p.add_argument("--reroll", type=int, default=0, help="pick another background stretch (1, 2, ...)")
    p.add_argument("--clip", help="use only this background clip (file name in backgrounds_dir)")
    args = p.parse_args()

    cfg = load_config(args.config)
    base = args.config.resolve().parent
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    paths = cfg["paths"]
    ffmpeg, ffprobe = tool("ffmpeg", paths.get("ffmpeg_dir", "")), tool("ffprobe", paths.get("ffmpeg_dir", ""))
    fonts_dir = str((base / paths["fonts_dir"]).resolve()) if paths.get("fonts_dir") else ""

    content = (base / paths["content_dir"]).resolve()
    if args.folder:
        folder = args.folder.resolve()
    elif args.date:
        folder = content / args.date
    else:
        days = sorted(d for d in content.iterdir() if (d / "captions.ass").exists()) if content.exists() else []
        if not days:
            sys.exit(f"no day in {content} has captions yet; run caption-maker first")
        folder = days[-1]
    for need, by in (("script.json", "script-writer"), ("voice.wav", "voice-generator"), ("timeline.json", "caption-maker"), ("captions.ass", "caption-maker")):
        if not (folder / need).exists():
            sys.exit(f"{folder / need} missing; run {by} first")
    script = json.loads((folder / "script.json").read_text(encoding="utf-8"))
    timeline = json.loads((folder / "timeline.json").read_text(encoding="utf-8"))
    total = round(probe_seconds(ffprobe, folder / "voice.wav") + cfg["video"]["tail"], 3)

    # date + title: a rerun of the same script gives the same reel, a new script on the same day gets a new stretch
    rng = random.Random(f"{script['date']}/{script.get('title', '')}/{args.reroll}")
    clip, offset = pick_background((base / paths["backgrounds_dir"]).resolve(), ffprobe, total, cfg["background"]["margin"], rng, args.clip,
                                   ffmpeg, cfg["background"], log)
    log(f"folder: {folder}; {total:.1f} s; background {clip.name} from {offset:.1f} s")

    cast = {c["id"]: c for c in script["cast"]}
    cc = cfg["characters"]
    with tempfile.TemporaryDirectory(prefix="composer-") as tmp:
        images = {}
        for sid, c in cast.items():
            pack = cfg["packs"].get(c["pack"], {})
            # face the middle: mirror when the character's natural facing points off-screen
            flip = c["faces"] == c["position"]
            args_ = ((base / paths["packs_dir"] / c["pack"]).resolve(), {cc["normal"], cc["shocked"], cc["idle"]})
            images[sid] = prepare_pack(*args_, cc["height"], flip, pack.get("no_flip", []), Path(tmp), cc["idle"])
            # a figure wider than its share would be cut by the frame edge (Arjun's face was, at 600 px): shrink it so
            # at most `bleed` of the figure is off-screen
            bl, br = images[sid]["_body"]
            fit = cc["inner_edge"] * cfg["video"]["width"] / (1 - cc.get("bleed", 0.15))
            if br - bl > fit:
                h = int(cc["height"] * fit / (br - bl))
                images[sid] = prepare_pack(*args_, h, flip, pack.get("no_flip", []), Path(tmp), cc["idle"])
            log(f"  {c['name']}: {c['pack']} on the {c['position']}{' (mirrored)' if flip else ''}, {images[sid][cc['normal']][1]}x{images[sid][cc['normal']][2]}")
        turns = plan_turns(timeline, total, cc)
        overlays = build_overlays(turns, cast, images, cfg)
        for t in turns:
            log(f"  {t['in']:6.2f}-{t['end']:6.2f}  {cast[t['speaker']]['name']} ({len(t['lines'])} line{'s' if len(t['lines']) > 1 else ''})")
        overlay_ass = None
        cues: list[tuple[str, float]] = []
        if cfg.get("overlay", {}).get("enabled", True) and cfg.get("overlay"):
            text, cues = make_overlay(script, timeline, cfg)
            (folder / "overlay.ass").write_text(text, encoding="utf-8")
            overlay_ass = "overlay.ass"
        sounds = sound_inputs(cfg, base, turns, cues, total, log)
        panels = plan_panels(folder, timeline, total, cfg)
        if panels:
            log(f"  {len(panels)} screenshot panel(s) from visuals.json")
        out = render(ffmpeg, folder, clip, offset, total, overlays, cfg, fonts_dir, log, sounds, overlay_ass, panels)

    # the Instagram cover: a frame with the whole hook card on it (its words have landed, it hasn't faded yet); frame 0
    # shows only the first word ("DIN", 2026-11-02 auto post)
    o, cover = cfg.get("overlay", {}), 0.0
    if overlay_ass and script.get("hook_card") and timeline.get("lines"):
        hook_end = max(o["hook_hold"], timeline["lines"][0]["end"])
        cover = round(max(0.0, min(o["hook_build"] + o.get("cover_after_build", 0.3), hook_end - 0.2)), 2)
    (folder / "compose.json").write_text(json.dumps({
        "reel": out.name, "seconds": total, "cover_seconds": cover,
        "background": {"clip": clip.name, "offset": offset, "reroll": args.reroll},
        "turns": [{"speaker": t["speaker"], "in": round(t["in"], 3), "end": round(t["end"], 3), "lines": len(t["lines"])} for t in turns],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2) + "\n", encoding="utf-8")
    log(f"wrote {out} and compose.json")


if __name__ == "__main__":
    main()
