"""
normalize-video: turns raw recordings into clips of one fixed size, frame rate and format. See README.md.

    python tools/normalize-video/normalize_video.py [files...] [--config FILE] [--input DIR] [--output DIR] [--force]
           [--width N] [--height N] [--fit crop|pad] [--fps N] [--crf N] [--preset NAME]
           [--keep-audio | --no-audio] [--trim-start S] [--trim-end S] [--margin PX]

Settings come from config.toml (next to this script unless --config says otherwise); flags override them for one run.
Paths in the config are relative to the config file; paths given as flags are relative to the current directory.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import os
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"
SECTION = "normalize"  # this tool's table in the master config and the mode files

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



# ---- settings ----

def load_settings(argv: list[str] | None = None) -> tuple[dict, list[str], bool]:
    p = argparse.ArgumentParser(description="Normalize video files to one size, frame rate and format.")
    p.add_argument("files", nargs="*", help="file names inside the input folder (default: all)")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="settings file (default: config.toml next to this script)")
    p.add_argument("--input", type=Path, help="input folder")
    p.add_argument("--output", type=Path, help="output folder")
    p.add_argument("--force", action="store_true", help="process every file even if its output is up to date")
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--fit", choices=["crop", "pad"])
    p.add_argument("--fps", type=float, help="0 keeps the source frame rate")
    p.add_argument("--crf", type=int)
    p.add_argument("--preset")
    audio = p.add_mutually_exclusive_group()
    audio.add_argument("--keep-audio", dest="keep_audio", action="store_true", default=None)
    audio.add_argument("--no-audio", dest="keep_audio", action="store_false")
    p.add_argument("--trim-start", type=float)
    p.add_argument("--trim-end", type=float)
    p.add_argument("--margin", type=int)
    args = p.parse_args(argv)

    if not args.config.exists():
        sys.exit(f"config not found: {args.config}")
    cfg = load_config(args.config)
    base = args.config.resolve().parent
    paths, video, audio_cfg, trim = cfg.get("paths", {}), cfg.get("video", {}), cfg.get("audio", {}), cfg.get("trim", {})

    def path_from(flag: Path | None, key: str) -> Path:
        if flag is not None:
            return flag.resolve()
        if key not in paths:
            sys.exit(f"{key} is not set: give --{key.split('_')[0]} or set [paths] {key} in {args.config}")
        return (base / paths[key]).resolve()

    pick = lambda flag, value, default: flag if flag is not None else value if value is not None else default  # noqa: E731
    s = {
        "input_dir": path_from(args.input, "input_dir"),
        "output_dir": path_from(args.output, "output_dir"),
        "manifest": paths.get("manifest", "manifest.json"),
        "extensions": {e.lower() for e in paths.get("extensions", [".mp4", ".mkv", ".mov", ".webm"])},
        "width": pick(args.width, video.get("width"), 1080),
        "height": pick(args.height, video.get("height"), 1920),
        "fit": pick(args.fit, video.get("fit"), "crop"),
        "pad_color": video.get("pad_color", "black"),
        "fps": pick(args.fps, video.get("fps"), 30),
        "crf": pick(args.crf, video.get("crf"), 28),
        "preset": pick(args.preset, video.get("preset"), "medium"),
        "keep_audio": pick(args.keep_audio, audio_cfg.get("keep"), False),
        "audio_bitrate": audio_cfg.get("bitrate", "128k"),
        "trim_start": pick(args.trim_start, trim.get("start"), 0.0),
        "trim_end": pick(args.trim_end, trim.get("end"), 0.0),
        "margin": pick(args.margin, trim.get("margin"), 0),
    }
    if s["fit"] not in ("crop", "pad"):
        sys.exit(f'fit must be "crop" or "pad", got "{s["fit"]}"')
    if s["width"] % 2 or s["height"] % 2:
        sys.exit("width and height must be even numbers (H.264 needs it)")
    return s, args.files, args.force


def signature(s: dict) -> dict:
    """The settings that change the output: a file made with different ones is redone."""
    keys = ["width", "height", "fit", "pad_color", "fps", "crf", "preset", "keep_audio", "audio_bitrate", "trim_start", "trim_end", "margin"]
    return {k: s[k] for k in keys}


# ---- ffmpeg ----

def tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        sys.exit(f"{name} not found on PATH. Install FFmpeg (Windows: winget install --id Gyan.FFmpeg -e) and open a new terminal.")
    return path


def probe(path: Path) -> dict:
    out = subprocess.run(
        [tool("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type,width,height,avg_frame_rate:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    video = next((st for st in data.get("streams", []) if st.get("codec_type") == "video"), None)
    if not video:
        raise ValueError(f"{path.name}: no video stream")
    num, _, den = video.get("avg_frame_rate", "0/1").partition("/")
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": float(num) / float(den or 1) if float(den or 1) else 0.0,
        "duration": float(data["format"]["duration"]),
        "has_audio": any(st.get("codec_type") == "audio" for st in data["streams"]),
    }


def even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def build_filter(width: int, height: int, s: dict) -> str:
    m = s["margin"]
    w, h = even(width - 2 * m), even(height - 2 * m)
    steps = [f"crop={w}:{h}:{m}:{m}"] if m else []
    tw, th = s["width"], s["height"]
    if s["fit"] == "crop":
        # the largest centred region with the target's shape, then scaled to the target size
        cw, ch = (even(h * tw / th), h) if w / h > tw / th else (w, even(w * th / tw))
        steps.append(f"crop={cw}:{ch}:(iw-{cw})/2:(ih-{ch})/2")
        steps.append(f"scale={tw}:{th}:flags=lanczos")
    else:
        steps.append(f"scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos")
        steps.append(f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color={s['pad_color']}")
    if s["fps"]:
        steps.append(f"fps={s['fps']:g}")
    steps.append("setsar=1,format=yuv420p")
    return ",".join(steps)


def run_ffmpeg(cmd: list[str], length: float) -> int:
    """Runs ffmpeg with -progress on stdout and prints a line every few seconds."""
    began, last = time.time(), 0.0
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True) as proc:
        for line in proc.stdout:
            if not line.startswith("out_time_us="):
                continue
            try:
                done = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                continue
            now = time.time()
            if now - last >= 5 and done > 0:
                last = now
                eta = max(0.0, (now - began) / done * (length - done))
                print(f"  {min(100.0, done / length * 100):5.1f}%  {done / 60:5.1f}/{length / 60:.1f} min  ETA {eta / 60:4.1f} min", flush=True)
    return proc.returncode


def process(src: Path, s: dict) -> dict:
    info = probe(src)
    length = info["duration"] - s["trim_start"] - s["trim_end"]
    if length < 1:
        raise ValueError(f"{src.name}: {length:.1f}s left after trimming {s['trim_start']}s + {s['trim_end']}s")
    vf = build_filter(info["width"], info["height"], s)
    dst = s["output_dir"] / f"{src.stem}.mp4"
    tmp = dst.with_suffix(".part.mp4")  # a half-written file never looks finished

    print(f"\n{src.name}: {info['width']}x{info['height']}, {info['fps']:g} fps, {info['duration'] / 60:.1f} min, audio={'yes' if info['has_audio'] else 'no'}")
    print(f"  -> {dst}\n     {vf}")
    audio = ["-c:a", "aac", "-b:a", s["audio_bitrate"]] if s["keep_audio"] else ["-an"]
    cmd = [
        tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(s["trim_start"]), "-i", str(src), "-t", f"{length:.3f}",
        "-vf", vf, *audio,
        "-c:v", "libx264", "-preset", s["preset"], "-crf", str(s["crf"]),
        "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
        str(tmp),
    ]
    began = time.time()
    if run_ffmpeg(cmd, length) != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{src.name}: ffmpeg failed")

    out = probe(tmp)
    problems = []
    if (out["width"], out["height"]) != (s["width"], s["height"]):
        problems.append(f"size {out['width']}x{out['height']}")
    if s["keep_audio"] and info["has_audio"] and not out["has_audio"]:
        problems.append("audio missing")
    if not s["keep_audio"] and out["has_audio"]:
        problems.append("audio still present")
    if abs(out["duration"] - length) > 2:
        problems.append(f"duration {out['duration']:.1f}s, expected {length:.1f}s")
    if problems:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{src.name}: output check failed: {', '.join(problems)}")
    tmp.replace(dst)

    mb_in, mb_out = src.stat().st_size / 1e6, dst.stat().st_size / 1e6
    print(f"  done in {(time.time() - began) / 60:.1f} min: {mb_in:,.0f} MB -> {mb_out:,.0f} MB")
    return {
        "file": dst.name,
        "source": src.name,
        "width": out["width"],
        "height": out["height"],
        "fps": round(out["fps"], 3),
        "duration": round(out["duration"], 2),
        "has_audio": out["has_audio"],
        "size_mb": round(mb_out, 1),
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settings": signature(s),
    }


# ---- main ----

def main() -> None:
    s, files, force = load_settings()
    inp, outp = s["input_dir"], s["output_dir"]
    if not inp.is_dir():
        sys.exit(f"input folder not found: {inp}")
    if inp == outp:
        sys.exit("input and output folders must differ")
    outp.mkdir(parents=True, exist_ok=True)

    if files:
        sources = [inp / f for f in files]
        missing = [src.name for src in sources if not src.exists()]
        if missing:
            sys.exit(f"not in {inp}: {', '.join(missing)}")
    else:
        sources = sorted(f for f in inp.iterdir() if f.is_file() and f.suffix.lower() in s["extensions"])
    if not sources:
        sys.exit(f"no videos in {inp}")

    manifest_path = outp / s["manifest"]
    entries: dict[str, dict] = {}
    if manifest_path.exists():
        entries = {c["file"]: c for c in json.loads(manifest_path.read_text(encoding="utf-8")).get("clips", [])}

    failed = 0
    for src in sources:
        dst = outp / f"{src.stem}.mp4"
        entry = entries.get(dst.name)
        fresh = dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime and entry is not None
        # an entry from before settings were recorded counts as made with the current ones
        if not force and fresh and entry.get("settings", signature(s)) == signature(s):
            print(f"{src.name}: up to date, skipped")
            continue
        try:
            entries[dst.name] = process(src, s)
        except Exception as err:  # one bad file should not stop the rest
            failed += 1
            print(f"  FAILED: {err}", file=sys.stderr)
        # saved after every file, so a later failure keeps what already finished
        manifest_path.write_text(json.dumps({"clips": sorted(entries.values(), key=lambda c: c["file"])}, indent=2) + "\n", encoding="utf-8")

    total = sum(c["duration"] for c in entries.values())
    print(f"\n{len(entries)} file(s) in {outp}, {total / 60:.1f} min in total. Manifest: {manifest_path}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
