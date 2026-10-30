"""
voice audition: measures how high or low each Gemini voice sits, so the cast can be picked by ear AND by numbers.
Part of voice-generator; settings in config.toml [audition]. See README.md.

    python tools/voice-generator/audition.py                 # audition the voices in [audition].voices not measured yet
    python tools/voice-generator/audition.py --measure FILE --lines LINES.json   # measure an existing dialogue, no API call

Each request is a two-voice dialogue (two voices per call), read with plain direction so the voice's own pitch shows.
Whisper times every line, and each voice's median pitch (F0) and range are saved to <out_dir>/audition.json, with the
audio next to it to listen to. A voice already in audition.json is never requested again (--redo to force).

Gentle on the free tier (2026-10-30, after the Gemini overload): one request at a time, at least `min_interval` seconds
apart, a 429 waits for the time Google asks for, and a daily-quota 429 stops the run (progress is kept) instead of
retrying.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "caption-maker"))
import generate_voice as gv  # noqa: E402
import make_captions as mc  # noqa: E402

DEFAULT_CONFIG = HERE / "config.toml"


def f0_track(x, sr: int = 16000):
    """Pitch per 40 ms frame by autocorrelation (70-400 Hz), voiced frames only."""
    import numpy as np
    out = []
    for i in range(0, len(x) - 640, 320):
        fr = x[i:i + 640] * np.hanning(640)
        if np.sqrt((fr ** 2).mean()) < 0.02:
            continue
        ac = np.correlate(fr, fr, "full")[639:]
        lo, hi = sr // 400, sr // 70
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] > 0.3 * ac[0]:
            out.append(sr / k)
    return np.array(out)


def measure(ffmpeg: str, model, wav: Path, lines: list[dict]) -> dict[str, dict]:
    """lines: [{"speaker": label, "text": ...}] in order. Returns label -> {median_hz, p10_hz, p90_hz, frames}."""
    import numpy as np
    audio = mc.load_audio(ffmpeg, wav)
    script = {"lines": [{"speaker": ln["speaker"], "name": ln["speaker"], "text": ln["text"]} for ln in lines]}
    timeline, rate = mc.build_timeline(script, mc.heard_words(model, audio, None), len(audio) / 16000)
    per: dict[str, list] = {}
    for ln in timeline["lines"]:
        per.setdefault(ln["speaker"], []).append(f0_track(audio[int(ln["start"] * 16000):int(ln["end"] * 16000)]))
    out = {}
    for label, parts in per.items():
        a = np.concatenate(parts) if parts else np.array([])
        if len(a) < 20:
            continue
        out[label] = {"median_hz": round(float(np.median(a)), 1), "p10_hz": round(float(np.percentile(a, 10))),
                      "p90_hz": round(float(np.percentile(a, 90))), "frames": int(len(a)), "match": round(rate, 2)}
    return out


def daily_quota(detail: str) -> bool:
    return bool(re.search(r"PerDay|per day|RequestsPerDay", detail, re.I))


def retry_after(detail: str, default: float) -> float:
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', detail)
    return float(m.group(1)) if m else default


def main() -> None:
    p = argparse.ArgumentParser(description="Measure the pitch of Gemini voices.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--redo", action="store_true", help="request voices again even if already measured")
    p.add_argument("--measure", type=Path, help="measure an existing dialogue wav instead (no API call)")
    p.add_argument("--lines", type=Path, help="with --measure: JSON list of {speaker, text}, speaker = the voice name")
    p.add_argument("--profile", help="a casting brief from [audition.profiles.<name>]: its own direction, lines and voices")
    args = p.parse_args()

    cfg = gv.load_config(args.config)
    a = cfg["audition"]
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    out_dir = (HERE / a["out_dir"]).resolve()
    if args.profile:
        # e.g. "oldman" (2026-10-30: Verma on Rasalgethi didn't sound like an old man): same pitch measuring, but the
        # voices are asked to play the part, and the results go to their own folder
        a = {**a, **a["profiles"][args.profile]}
        out_dir = out_dir / args.profile
    out_dir.mkdir(parents=True, exist_ok=True)
    # a profile ("oldman") measures voices playing a part, so its pitches go to their own file
    results_path = HERE / (a["results"] if not args.profile else a["results"].replace(".json", f"-{args.profile}.json"))
    results = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else {}
    ffmpeg = gv.find_ffmpeg(cfg["paths"].get("ffmpeg", ""))
    model = mc.load_model({"model": "base.en", "device": "cpu", "compute_type": "int8"}, log)

    def save():
        results_path.write_text(json.dumps(dict(sorted(results.items())), indent=2) + "\n", encoding="utf-8")

    if args.measure:
        lines = json.loads(args.lines.read_text(encoding="utf-8"))
        for voice, stats in measure(ffmpeg, model, args.measure, lines).items():
            results[voice] = {**stats, "source": str(args.measure.name)}
            log(f"{voice:14} {stats['median_hz']:6.1f} Hz  ({stats['p10_hz']}-{stats['p90_hz']})")
        save()
        return

    gv.load_env_files([(HERE / f).resolve() for f in cfg["tts"].get("env_files", [])])
    key = os.environ.get(cfg["tts"]["key_env"], "").strip()
    if not key:
        sys.exit(f"API key missing: set {cfg['tts']['key_env']}")
    todo = [v for v in a["voices"] if args.redo or v not in results]
    pairs = [todo[i:i + 2] for i in range(0, len(todo), 2)]
    log(f"{len(todo)} voice(s) to audition in {len(pairs)} request(s), at least {a['min_interval']} s apart")
    last = 0.0
    tts_models = list(cfg["tts"]["models"])  # a daily-quota 429 moves on to the next model (each has its own quota)
    for pair in pairs:
        if len(pair) == 1:  # a dialogue needs two voices: pair the odd one with a measured voice
            pair = pair + [next((v for v in results if v not in pair), a["voices"][0])]
        lines = [{"speaker": pair[k % 2], "text": t} for k, t in enumerate(a["lines"])]
        cast = {v: {"label": v, "voice": v} for v in pair}
        prompt = a["direction"] + "\n\n" + "\n".join(f"{ln['speaker']}: {ln['text']}" for ln in lines)
        wav = out_dir / f"{pair[0]}_{pair[1]}.wav"
        for attempt in (1, 2):
            wait = a["min_interval"] - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            last = time.time()
            try:
                tagged = [(ln["speaker"], f"{a.get('tag', '')} {ln['text']}".strip()) for ln in lines]
                pcm = gv.call_tts(tts_models[0], key, prompt, cast, cfg["tts"]["timeout_seconds"], tagged, cfg)
                used_model = tts_models[0]
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                if e.code == 429 and daily_quota(detail):
                    tts_models.pop(0)
                    if not tts_models:
                        save()
                        sys.exit(f"daily TTS quota used up on every model; stopped. {len(results)} voice(s) measured so far.")
                    log(f"daily quota used up, switching to {tts_models[0]}")
                    continue
                if e.code in (429, 500, 503) and attempt == 1:
                    delay = retry_after(detail, 60)
                    log(f"{'+'.join(pair)}: HTTP {e.code}, waiting {delay:.0f} s as asked")
                    time.sleep(delay)
                    continue
                save()
                sys.exit(f"{'+'.join(pair)}: HTTP {e.code} {detail[:200]}")
            except Exception as e:  # no audio, timeout, dropped connection: one retry
                log(f"{'+'.join(pair)}: {e!r}"[:200])
                if attempt == 2:
                    pcm = None
        else:
            pcm = None
        if not pcm:
            log(f"{'+'.join(pair)}: no audio, skipped")
            continue
        gv.write_wav(wav, pcm)
        # also at the reel's speed, so the voice is judged as it will be heard
        sped = wav.with_name(f"{wav.stem}_{cfg['voice']['speed']}x.wav")
        subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(wav), "-af", f"atempo={cfg['voice']['speed']}", str(sped)])
        for voice, stats in measure(ffmpeg, model, wav, lines).items():
            if voice in pair:
                results[voice] = {**stats, "source": wav.name, "model": used_model}
                log(f"{voice:14} {stats['median_hz']:6.1f} Hz  ({stats['p10_hz']}-{stats['p90_hz']})  {wav.name}")
        save()
    log(f"wrote {results_path}")
    for v, s in sorted(results.items(), key=lambda kv: kv[1]["median_hz"]):
        log(f"  {s['median_hz']:6.1f} Hz  {v}")


if __name__ == "__main__":
    main()
