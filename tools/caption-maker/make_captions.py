"""
caption-maker: times the script's words against the voice and makes per-speaker captions. See README.md.

    python tools/caption-maker/make_captions.py [--date YYYY-MM-DD | --script FILE] [--audio FILE | --video FILE] [--config FILE]

Writes next to the script:
  timeline.json  when each line and word is spoken (the composer slides the characters in and out with it)
  captions.ass   2-3 word captions, coloured per speaker, the spoken word highlighted
With --video, the captions are also burned in: <video name>.captioned.mp4 next to the script.
The text always comes from the script; Whisper (faster-whisper) is only used for the timing, because it mishears
names. Script words Whisper didn't hear get times spread between the nearest words it did.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"
SECTION = "captions"  # this tool's table in the master config and the mode files

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



def find_ffmpeg(configured: str) -> str:
    path = configured or shutil.which("ffmpeg")
    if not path or (not Path(path).exists() and not shutil.which(path)):
        sys.exit("ffmpeg not found. Install FFmpeg (Windows: winget install --id Gyan.FFmpeg -e) and open a new terminal, or set paths.ffmpeg.")
    return path


# ---- timing ----

def load_audio(ffmpeg: str, path: Path):
    """16 kHz mono float32, decoded by ffmpeg (faster-whisper's own PyAV decoder fails here: 'metadata_errors')."""
    import numpy as np
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        sys.exit(f"could not read audio from {path}: {r.stderr.decode('utf-8', 'replace').strip()[:300] or 'no audio stream'}")
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


def load_model(w: dict, log):
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from faster_whisper import WhisperModel
    t = time.time()
    try:  # the cached copy, without the Hugging Face network checks that took ~3 min (2026-10-29)
        model = WhisperModel(w["model"], device=w["device"], compute_type=w["compute_type"], local_files_only=True)
    except Exception:
        log(f"whisper: {w['model']} not cached yet, downloading once")
        model = WhisperModel(w["model"], device=w["device"], compute_type=w["compute_type"])
    log(f"whisper: {w['model']} loaded in {time.time() - t:.1f} s")
    return model


def heard_words(model, audio, prompt: str | None, language: str = "en") -> list[dict]:
    segments, _ = model.transcribe(audio, language=language, word_timestamps=True, initial_prompt=prompt, vad_filter=False, beam_size=5)
    return [{"word": w.word.strip(), "start": w.start, "end": w.end} for s in segments for w in (s.words or [])]


def norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def align(script_words: list[str], heard: list[dict], total: float) -> tuple[list[dict], float]:
    """Gives every script word a start/end. Matched words take Whisper's times; runs of unmatched words share the
    gap between their matched neighbours in proportion to their length."""
    a, b = [norm(w) for w in script_words], [norm(h["word"]) for h in heard]
    times: list[dict | None] = [None] * len(script_words)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            h = heard[blk.b + k]
            times[blk.a + k] = {"start": h["start"], "end": h["end"], "matched": True}
    matched = sum(1 for t in times if t)
    first = heard[0]["start"] if heard else 0.0
    last = heard[-1]["end"] if heard else total
    i = 0
    while i < len(times):
        if times[i]:
            i += 1
            continue
        j = i
        while j < len(times) and not times[j]:
            j += 1
        lo = times[i - 1]["end"] if i > 0 else first
        hi = times[j]["start"] if j < len(times) else last
        if hi < lo:
            hi = lo
        weights = [max(len(a[k]), 1) for k in range(i, j)]
        span, at = hi - lo, lo
        for k, wgt in zip(range(i, j), weights):
            d = span * wgt / sum(weights)
            times[k] = {"start": at, "end": at + d, "matched": False}
            at += d
        i = j
    prev = 0.0
    for t in times:  # never backwards
        t["start"] = max(t["start"], prev)
        t["end"] = max(t["end"], t["start"])
        prev = t["start"]
    return times, (matched / len(times) if times else 0.0)


def build_timeline(script: dict, heard: list[dict], total: float) -> tuple[dict, float]:
    flat = [(li, w) for li, ln in enumerate(script["lines"]) for w in ln["text"].split()]
    times, rate = align([w for _, w in flat], heard, total)
    lines = []
    for li, ln in enumerate(script["lines"]):
        words = [{"word": w, "start": round(t["start"], 3), "end": round(t["end"], 3), "matched": t["matched"]}
                 for (lj, w), t in zip(flat, times) if lj == li]
        lines.append({
            "index": li, "speaker": ln["speaker"], "name": ln.get("name", ln["speaker"]), "expression": ln.get("expression", "normal"),
            "text": ln["text"], "start": words[0]["start"], "end": words[-1]["end"], "words": words,
        })
    return {"seconds": round(total, 3), "match": round(rate, 3), "lines": lines}, rate


# ---- captions ----

def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def alpha(opacity_percent: float) -> int:
    """ASS alpha is transparency: 0 = solid, 255 = invisible."""
    return round(255 * (1 - max(0, min(100, opacity_percent)) / 100))


def ass_time(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def chunks_of(timeline: dict, c: dict) -> list[dict]:
    out = []
    for ln in timeline["lines"]:
        cur: list[dict] = []
        for w in ln["words"]:
            cur.append(w)
            if len(cur) >= c["max_words"] or re.search(r"[.,!?;:]$", w["word"]):
                out.append({"speaker": ln["speaker"], "words": cur})
                cur = []
        if cur:
            out.append({"speaker": ln["speaker"], "words": cur})
    for k, ch in enumerate(out):
        ch["start"] = ch["words"][0]["start"]
        ch["end"] = ch["words"][-1]["end"]
        nxt = out[k + 1]["words"][0]["start"] if k + 1 < len(out) else None
        if nxt is not None and nxt - ch["end"] < c["hold_gap"]:
            ch["end"] = nxt
    return out


def clean(word: str, upper: bool) -> str:
    word = word.replace("\\", "").replace("{", "(").replace("}", ")")
    return word.upper() if upper else word


def make_ass(timeline: dict, cfg: dict) -> str:
    c, v = cfg["captions"], cfg["video"]
    speakers = {ln["speaker"] for ln in timeline["lines"]}
    look = c["look"]
    hexes = {sid: cfg["speakers"].get(sid, cfg["speakers"].get("default", "#212121")) for sid in speakers}
    colors = {sid: ass_color(h) for sid, h in hexes.items()}
    styles = []
    for sid in sorted(speakers):
        # box: OutlineColour/BackColour fill the box. Other looks: they colour the outline and the drop shadow.
        col = colors[sid]
        back = ass_color(hexes[sid], alpha(c.get("shadow_opacity", 100))) if look == "glow" else col
        shadow = c["shadow"] if look in ("shadow", "glow") else 0
        styles.append(
            f"Style: {sid},{c['font']},{c['size']},{ass_color(c['text_color'])},{ass_color(c['text_color'])},{col},{back},"
            f"{-1 if c['bold'] else 0},0,0,0,100,100,0,0,{3 if look == 'box' else 1},{c['border']},{shadow},2,"
            f"{c['margin_sides']},{c['margin_sides']},{c['margin_bottom']},1"
        )
    hi, base = ass_color(c["highlight_color"]), ass_color(c["text_color"])
    pop = c["pop_from"] < 100
    blur = f"{{\\blur{c['blur']}}}" if c.get("blur") else ""
    events = []
    for ch in chunks_of(timeline, c):
        ws = ch["words"]
        plain = " ".join(clean(x["word"], c["uppercase"]) for x in ws)
        for i, w in enumerate(ws):
            start = ch["start"] if i == 0 else w["start"]
            end = ws[i + 1]["start"] if i + 1 < len(ws) else ch["end"]
            if end - start < 0.01:
                continue
            text = " ".join(
                f"{{\\c{hi}}}{clean(x['word'], c['uppercase'])}{{\\c{base}}}" if j == i else clean(x["word"], c["uppercase"])
                for j, x in enumerate(ws)
            )
            grow = f"{{\\fscx{c['pop_from']}\\fscy{c['pop_from']}\\t(0,{c['pop_ms']},\\fscx100\\fscy100)}}" if pop and i == 0 else ""
            when = f"{ass_time(start)},{ass_time(end)},{ch['speaker']},,0,0,0,"
            if look == "glow":  # a soft blurred copy in the speaker's colour behind the crisp white text
                col, a = colors[ch["speaker"]], f"&H{alpha(c['glow_opacity']):02X}&"
                events.append(f"Dialogue: 0,{when},{{\\1c{col}\\3c{col}\\1a{a}\\3a{a}\\bord{c['glow_size']}\\shad0\\blur{c['glow_blur']}}}{grow}{plain}")
            events.append(f"Dialogue: 1,{when},{blur}{grow}{text}")
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {v['width']}", f"PlayResY: {v['height']}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        *styles, "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events, "",
    ])


def burn(ffmpeg: str, video: Path, ass: Path, out: Path, fonts_dir: str) -> None:
    """Runs in the captions' folder so the subtitles filter gets a bare file name (Windows drive colons break it)."""
    vf = f"subtitles={ass.name}"
    if fonts_dir:
        vf += ":fontsdir=" + Path(os.path.relpath(fonts_dir, ass.parent)).as_posix()  # relative: a drive colon breaks the filter
    part = out.with_suffix(".part.mp4")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video.resolve()), "-vf", vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
           # stereo AAC 48 kHz: what Instagram expects, and plays everywhere (a mono track was silent in the user's player)
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(part.resolve())]
    r = subprocess.run(cmd, cwd=ass.parent, capture_output=True, text=True)
    if r.returncode != 0:
        part.unlink(missing_ok=True)
        sys.exit(f"ffmpeg failed to burn the captions: {r.stderr.strip()[:500]}")
    part.replace(out)


def main() -> None:
    p = argparse.ArgumentParser(description="Time the script against the voice and make captions.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--date", help="YYYY-MM-DD (default: the latest script)")
    p.add_argument("--script", type=Path, help="a script.json anywhere (output goes next to it)")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--audio", type=Path, help="audio to time against (default: paths.default_audio next to the script)")
    src.add_argument("--video", type=Path, help="video to time against and burn the captions into")
    args = p.parse_args()

    cfg = load_config(args.config)
    base = args.config.resolve().parent
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731

    content = (base / cfg["paths"]["content_dir"]).resolve()
    if args.script:
        script_path = args.script.resolve()
    elif args.date:
        script_path = content / args.date / "script.json"
    else:
        days = sorted(d for d in content.iterdir() if (d / "script.json").exists()) if content.exists() else []
        if not days:
            sys.exit(f"no scripts in {content}; run script-writer first")
        script_path = days[-1] / "script.json"
    if not script_path.exists():
        sys.exit(f"script not found: {script_path}")
    script = json.loads(script_path.read_text(encoding="utf-8"))
    folder = script_path.parent
    media = (args.video or args.audio or folder / cfg["paths"]["default_audio"]).resolve()
    if not media.exists():
        sys.exit(f"not found: {media} (run voice-generator first, or pass --audio/--video)")
    ffmpeg = find_ffmpeg(cfg["paths"].get("ffmpeg", ""))
    fonts_dir = str((base / cfg["paths"]["fonts_dir"]).resolve()) if cfg["paths"].get("fonts_dir") else ""

    audio = load_audio(ffmpeg, media)
    total = len(audio) / 16000
    log(f"script: {script_path} ({len(script['lines'])} lines); timing against {media.name} ({total:.1f} s)")
    w = cfg["whisper"]
    model = load_model(w, log)
    # Plain first; with the script as a hint too when enabled, keeping whichever matches the script better.
    # The hint can make Whisper skip the opening lines (2026-10-29: it started at 3.96 s), so it never runs alone.
    prompts = [None] + ([" ".join(ln["text"] for ln in script["lines"])] if w["try_script_prompt"] else [])
    best = None
    for prompt in prompts:
        if prompt and best[1] >= w["good_match"]:  # the plain pass is good enough; the hint pass once took 139 s
            break
        t = time.time()
        heard = heard_words(model, audio, prompt, w.get("language", "en"))
        timeline, rate = build_timeline(script, heard, total)
        log(f"whisper{' (script hint)' if prompt else ''}: heard {len(heard)} words in {time.time() - t:.1f} s, {rate:.0%} of the script matched")
        if best is None or rate > best[1]:
            best = (timeline, rate)
    timeline, rate = best
    timeline = {"script": script_path.name, "media": media.name, **timeline, "whisper_model": w["model"],
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if rate < w["min_match"]:
        log(f"WARNING: only {rate:.0%} of the script's words were matched; the audio may not follow the script, check the timing")
    (folder / "timeline.json").write_text(json.dumps(timeline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ass_path = folder / "captions.ass"
    ass_path.write_text(make_ass(timeline, cfg), encoding="utf-8")
    for ln in timeline["lines"]:
        log(f"  {ln['start']:6.2f}-{ln['end']:6.2f}  {ln['name']}: {ln['text']}")
    log(f"wrote timeline.json ({rate:.0%} of words matched) and captions.ass in {folder}")

    if args.video:
        out = folder / f"{media.stem}.captioned.mp4"
        t = time.time()
        burn(ffmpeg, media, ass_path, out, fonts_dir)
        log(f"wrote {out} in {time.time() - t:.0f} s")


if __name__ == "__main__":
    main()
