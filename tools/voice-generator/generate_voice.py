"""
voice-generator: turns a day's script.json into the reel's voice track with Gemini TTS. See README.md.

    python tools/voice-generator/generate_voice.py [--date YYYY-MM-DD | --script FILE] [--config FILE] [--model NAME] [--new]

Writes next to the script:
  voice.raw.wav  what Gemini returned (1x), kept so a speed or loudness change doesn't need a new ~3 minute call
  voice.wav      the final track: sped up (pitch kept) and loudness-normalised
  voice.json     model, voice per character, durations, and a hash of the lines the raw audio was made from
The whole dialogue is one multi-speaker request, so the two characters react to each other; every character always
gets the same voice and notes from the config. When voice.raw.wav was made from the same lines and voices it is reused
(only the ffmpeg step runs again) unless --new is given.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"
SECTION = "voice"  # this tool's table in the master config and the mode files

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


# ---- request ----

def cast_of(script: dict, cfg: dict) -> dict[str, dict]:
    """Speaker id -> its config entry, in the order they first speak."""
    out = {}
    for ln in script["lines"]:
        sid = ln["speaker"]
        if sid in out:
            continue
        c = cfg["characters"].get(sid)
        if not c:
            sys.exit(f"no voice for character {sid!r}: add [characters.{sid}] to the config")
        out[sid] = c
    if len({c["voice"] for c in out.values()}) < len(out):
        sys.exit("two characters in this script share a voice; give each its own in the config")
    return out


def pitch_warnings(cast: dict, cfg: dict, base: Path) -> list[str]:
    """Two voices too close in pitch sound like one person (2026-10-30: Puck 121 Hz vs Gacrux 122 Hz)."""
    a = cfg.get("audition", {})
    f = (base / a.get("results", "voice-pitch.json")).resolve()
    if not f.exists():
        return []
    hz = {v: s["median_hz"] for v, s in json.loads(f.read_text(encoding="utf-8")).items()}
    out, items = [], list(cast.values())
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            x, y = hz.get(items[i]["voice"]), hz.get(items[j]["voice"])
            if not x or not y:
                out.append(f"{items[i]['voice']} or {items[j]['voice']} has no pitch in voice-pitch.json; run audition.py")
                continue
            gap = abs(12 * math.log2(x / y))
            if gap < a.get("min_gap_semitones", 3):
                out.append(f"{items[i]['label']} ({items[i]['voice']}, {x:.0f} Hz) and {items[j]['label']} ({items[j]['voice']}, {y:.0f} Hz) "
                           f"are only {gap:.1f} semitones apart; they will sound alike")
    return out


def build_prompt(script: dict, cast: dict, cfg: dict) -> str:
    v = cfg["voice"]
    notes = [v["direction"]] + [c["notes"] for c in cast.values()]
    shocked = [f'"{ln["text"]}"' for ln in script["lines"] if ln.get("expression") == "shocked"]
    if shocked and v.get("shocked"):
        notes.append(v["shocked"].format(lines=", ".join(shocked)))
    transcript = [f"{cast[ln['speaker']]['label']}: {ln['text']}" for ln in script["lines"]]
    return "\n".join(notes) + "\n\n" + "\n".join(transcript)


def per_line(model: str, cfg: dict | None) -> bool:
    """The gemini-3.x TTS models take one part per line (speaker in speechMetadata, style as an inline [tag]) and
    reject director notes; gemini-2.5 takes the whole transcript with notes as one text. A newer generation found by
    resolve_models (gemini-4...) is assumed to work like 3.x."""
    pats = (cfg or {}).get("tts", {}).get("per_line_models", ["gemini-3"])
    return any(model.startswith(p) for p in pats) or not model.startswith(("gemini-2", "gemini-1"))


GEMINI_TTS = re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash(-lite)?(?:-preview)?-tts(?:-preview)?$")


def resolve_models(models: list[str], key: str, cfg: dict, log, specs: dict | None = None, trail: list | None = None) -> list[str]:
    """Meant to run for years untouched (user, 2026-11-04), while Google retires models: configured TTS models no longer
    listed are dropped, and up to tts.discover_newer flash TTS models newer than the newest configured one are added at
    the end. If the listing fails, the configured list is used as is. specs gets model -> specs (for the report),
    trail a {model, outcome} for each dropped model."""
    try:
        names, token = {}, ""
        while True:
            req = urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000" + (f"&pageToken={token}" if token else ""),
                headers={"x-goog-api-key": key, "User-Agent": "video-generator/1.0 (voice-generator)"})
            data = with_deadline(lambda: json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8")), 40)
            names |= {m["name"].split("/")[-1]: {"provider": "Google Gemini", "name": m.get("displayName"), "version": m.get("version"),
                                                  "input_tokens": m.get("inputTokenLimit"), "output_tokens": m.get("outputTokenLimit")}
                      for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])}
            token = data.get("nextPageToken")
            if not token:
                break
    except Exception as e:
        log(f"model list failed ({e!r}"[:150] + "); using the configured TTS models")
        return models
    if specs is not None:
        specs.update({n: {k: v for k, v in s.items() if v is not None} for n, s in names.items()})
    out = [m for m in models if m in names]
    for m in models:
        if m not in names:
            log(f"{m}: no longer offered, skipped (update the config)")
            if trail is not None:
                trail.append({"model": m, "outcome": "no longer offered by the provider"})
    version = lambda n: (tuple(int(x) for x in GEMINI_TTS.match(n).group(1).split(".")), not GEMINI_TTS.match(n).group(2))  # noqa: E731
    newest = max((version(m) for m in models if GEMINI_TTS.match(m)), default=((0,), False))
    newer = sorted((n for n in names if GEMINI_TTS.match(n) and n not in out and version(n) > newest),
                   key=version, reverse=True)[: cfg["tts"].get("discover_newer", 2)]
    if newer:
        log(f"newer TTS models found, added as fallbacks: {', '.join(newer)}")
    return out + newer


def call_tts(model: str, key: str, prompt: str, cast: dict, timeout: int, lines: list[tuple[str, str]] | None = None,
             cfg: dict | None = None) -> bytes:
    """Returns 16-bit mono PCM at 24 kHz. Raises ValueError when the reply has no audio.
    lines: [(label, "[tags] text")] for the per-line models."""
    if per_line(model, cfg) and lines:
        contents = [{"role": "user", "parts": [{"text": t, "speechMetadata": {"speaker": s}} for s, t in lines]}]
    else:
        contents = [{"parts": [{"text": prompt}]}]
    body = {
        "contents": contents,
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
                {"speaker": c["label"], "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": c["voice"]}}} for c in cast.values()
            ]}},
        },
    }
    req = urllib.request.Request(
        API.format(model=model), data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key, "User-Agent": "video-generator/1.0 (voice-generator)"},
    )
    data = with_deadline(lambda: json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8")), timeout)
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    audio = [p["inlineData"] for p in parts if "inlineData" in p]
    if not audio:  # happens now and then (2026-10-29); a retry fixes it
        raise ValueError(f"no audio in the reply (finishReason={cand.get('finishReason')}, feedback={data.get('promptFeedback')})")
    raw = b"".join(base64.b64decode(a["data"]) for a in audio)
    if "wav" in audio[0].get("mimeType", ""):  # some models send a whole wav file instead of bare PCM
        with wave.open(io.BytesIO(raw)) as w:
            rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if (rate, ch, width) != (24000, 1, 2):
            raw = to_pcm24k(raw, rate, ch, width)
    return raw


def to_pcm24k(raw: bytes, rate: int, ch: int, width: int) -> bytes:
    """Any wav payload -> 16-bit mono 24 kHz, so every model's audio fits the same pipeline."""
    import subprocess as sp
    fmt = {1: "u8", 2: "s16le", 4: "s32le"}[width]
    r = sp.run(["ffmpeg", "-v", "error", "-f", fmt, "-ar", str(rate), "-ac", str(ch), "-i", "-", "-f", "s16le", "-ar", "24000", "-ac", "1", "-"],
               input=raw, capture_output=True)
    if r.returncode != 0:
        raise ValueError(f"could not convert {rate} Hz / {ch} ch audio: {r.stderr[:200]!r}")
    return r.stdout


def with_deadline(fn, seconds: float):
    """urlopen's timeout is per socket read, so a slow trickle can run far past it (Gemini calls hung 10+ min on
    2026-10-29). This caps the whole call; one that runs over is left in a daemon thread."""
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


def heartbeat(stop: threading.Event, log, label: str) -> None:
    start = time.time()
    while not stop.wait(30):
        log(f"{label}: still waiting, {time.time() - start:.0f} s")


def generate(script: dict, cast: dict, cfg: dict, key: str, models: list[str], log, trail: list | None = None) -> tuple[bytes, str]:
    """trail gets one {model, outcome} per model tried (why it was passed over, or 'used'), for the report."""
    trail = [] if trail is None else trail
    t = cfg["tts"]
    prompt = build_prompt(script, cast, cfg)
    words = sum(len(ln["text"].split()) for ln in script["lines"])
    # the per-line form for gemini-3.x: each character's style as an inline tag, plus a shock tag where marked
    tagged = [(cast[ln["speaker"]]["label"],
               " ".join(x for x in [cast[ln["speaker"]].get("tag", ""), cfg["voice"].get("shocked_tag", "") if ln.get("expression") == "shocked" else ""] if x)
               + " " + ln["text"]) for ln in script["lines"]]
    for model in models:
        outcome = ""  # why this model was passed over; the last reason wins
        for attempt in range(1, t["attempts"] + 1):
            label = f"{model} attempt {attempt}"
            log(f"{label}: asking for {words} words (usually ~3 min)")
            stop = threading.Event()
            threading.Thread(target=heartbeat, args=(stop, log, label), daemon=True).start()
            started = time.time()
            try:
                pcm = call_tts(model, key, prompt, cast, t["timeout_seconds"], tagged, cfg)
            except urllib.error.HTTPError as e:
                detail = e.read()[:600].decode("utf-8", "replace")
                log(f"{label}: HTTP {e.code} {detail.strip()[:200]}")
                outcome = {404: "not found (HTTP 404): retired or not open to this key", 429: "rate limited (HTTP 429)",
                           500: "server error (HTTP 500) after retries", 503: "overloaded (HTTP 503) after retries"}.get(e.code, f"HTTP {e.code}")
                if e.code == 429 and re.search(r"PerDay|per day", detail, re.I):
                    log(f"{label}: daily quota used up, trying the next model")  # each TTS model has its own quota
                    outcome = "free daily quota used up (HTTP 429)"
                    break
                if e.code in (429, 500, 503) and attempt < t["attempts"]:
                    time.sleep(20 * attempt)
                    continue
                break  # 404 (no such model), 400, or out of retries: next model
            except Exception as e:  # no audio, dropped connections, timeouts
                log(f"{label}: failed: {e!r}"[:300])
                outcome = "no audio in the reply" if isinstance(e, ValueError) else f"failed: {type(e).__name__}"
                time.sleep(5)
                continue
            finally:
                stop.set()
            seconds = len(pcm) / 48000
            wps = words / seconds if seconds else 0
            if not (t["min_words_per_second"] <= wps <= t["max_words_per_second"]):
                log(f"{label}: {seconds:.1f} s for {words} words ({wps:.1f}/s) looks cut off or garbled, retrying")
                outcome = "audio cut off or garbled"
                continue
            log(f"{label}: ok, {seconds:.1f} s of audio in {time.time() - started:.0f} s")
            trail.append({"model": model, "outcome": f"used (attempt {attempt})"})
            return pcm, model
        trail.append({"model": model, "outcome": outcome or "out of attempts"})
    sys.exit("no model returned usable audio")


# ---- files ----

def write_wav(path: Path, pcm: bytes, rate: int = 24000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def find_ffmpeg(configured: str) -> str:
    path = configured or shutil.which("ffmpeg")
    if not path or (not Path(path).exists() and not shutil.which(path)):
        sys.exit("ffmpeg not found. Install FFmpeg (Windows: winget install --id Gyan.FFmpeg -e) and open a new terminal, or set paths.ffmpeg.")
    return path


def pick_speed(v: dict, words: int, raw_seconds: float) -> float:
    """The speed-up that lands the voice at target_wps words per second, within [min_speed, max_speed]; the fixed
    `speed` when no target is set. Models talk at different rates (2026-10-30: gemini-3.8-flash-tts read 141 words in
    72 s, 2.5 in ~54 s), so a fixed 1.25x made the 3.8 reel 58 s and slow."""
    target = v.get("target_wps")
    if not target or not words or raw_seconds <= 0:
        return v["speed"]
    return round(min(v.get("max_speed", 1.6), max(v.get("min_speed", 1.15), target / (words / raw_seconds))), 3)


def finish_audio(ffmpeg: str, raw: Path, out: Path, v: dict, speed: float | None = None) -> None:
    """Speed-up with the pitch kept, then loudness normalisation."""
    filters = []
    speed = speed or v["speed"]
    if speed != 1:
        filters.append(f"atempo={speed}")
    if v.get("loudness_lufs"):
        filters.append(f"loudnorm=I={v['loudness_lufs']}:TP=-1.5:LRA=11")
    part = out.with_suffix(".part.wav")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw)]
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd += ["-ar", str(v["sample_rate"]), "-ac", "1", "-c:a", "pcm_s16le", str(part)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        part.unlink(missing_ok=True)
        sys.exit(f"ffmpeg failed: {r.stderr.strip()[:500]}")
    part.replace(out)


def lines_hash(cast: dict, prompt: str, model_list: list[str]) -> str:
    """Changes when anything that shapes the raw audio changes: lines, voices, notes or models."""
    key = json.dumps({"prompt": prompt, "voices": {k: c["voice"] for k, c in cast.items()}, "models": model_list}, sort_keys=True)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def main() -> None:
    p = argparse.ArgumentParser(description="Make the reel's voice track from a script.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--date", help="YYYY-MM-DD (default: the latest script)")
    p.add_argument("--script", type=Path, help="a script.json anywhere (output goes next to it)")
    p.add_argument("--model", help="use only this model")
    p.add_argument("--new", action="store_true", help="always ask Gemini again, even if voice.raw.wav matches the script")
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
    ffmpeg = find_ffmpeg(cfg["paths"].get("ffmpeg", ""))

    cast = cast_of(script, cfg)
    for w in pitch_warnings(cast, cfg, base):
        log(f"WARNING: {w}")
    models = [args.model] if args.model else cfg["tts"]["models"]
    prompt = build_prompt(script, cast, cfg)
    digest = lines_hash(cast, prompt, models)
    folder = script_path.parent
    raw_path, out_path, meta_path = folder / "voice.raw.wav", folder / "voice.wav", folder / "voice.json"
    old = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    log(f"script: {script_path} ({len(script['lines'])} lines, " + ", ".join(f"{c['label']}={c['voice']}" for c in cast.values()) + ")")

    reuse = not args.new and raw_path.exists() and old.get("hash") == digest
    specs, trail = {}, []
    if reuse:
        log("voice.raw.wav already matches this script; reusing it (use --new to ask Gemini again)")
        model = old.get("model")
        specs, trail = {model: old.get("spec", {})}, old.get("tried", [])
    else:
        load_env_files([(base / f).resolve() for f in cfg["tts"].get("env_files", [])])
        key = os.environ.get(cfg["tts"]["key_env"], "").strip()
        if not key:
            sys.exit(f"API key missing: set {cfg['tts']['key_env']} in the environment or in {cfg['tts'].get('env_files')}")
        # after the hash, so a changed model listing never forces a new (3 min) voice for an unchanged script
        tried = models if args.model else resolve_models(models, key, cfg, log, specs, trail)
        pcm, model = generate(script, cast, cfg, key, tried, log, trail)
        write_wav(raw_path, pcm)

    speed = pick_speed(cfg["voice"], sum(len(ln["text"].split()) for ln in script["lines"]), wav_seconds(raw_path))
    finish_audio(ffmpeg, raw_path, out_path, cfg["voice"], speed)
    meta = {
        "script": script_path.name,
        "model": model,
        # for the email report: the model's specs and why earlier models were passed over
        "spec": specs.get(model) or {"provider": "Google Gemini"},
        "tried": trail,
        "voices": {sid: {"label": c["label"], "voice": c["voice"]} for sid, c in cast.items()},
        "speed": speed,
        "loudness_lufs": cfg["voice"].get("loudness_lufs") or None,
        "raw_seconds": round(wav_seconds(raw_path), 2),
        "seconds": round(wav_seconds(out_path), 2),
        "hash": digest,
        "generated_at": old["generated_at"] if reuse else datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    log(f"wrote {out_path} ({meta['seconds']} s at {meta['speed']}x), voice.raw.wav ({meta['raw_seconds']} s) and voice.json")


if __name__ == "__main__":
    main()
