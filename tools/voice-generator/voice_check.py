"""Check a finished voice take, and rebuild a bad one from the day's other takes. No TTS calls.

Why (2026-11-27): Gemini's two-speaker TTS returned the 27 Nov reel with Arjun's first six lines in Riya's voice
(186-281 Hz; Arjun's Algenib is ~113 Hz). The captions matched only 62% of the words, the composer's timeline went
wrong (a caption frozen for 8 s, characters flickering in and out) and the reel was posted anyway. The trial and
YouTube takes of the same script, made in the same run, were fine (91%).

check_take   per line: the median pitch of the line (from timeline.json's line times) against each cast voice's
             measured pitch (voice-pitch.json). A line spoken nearer another cast voice is "wrong voice". The take
             fails when wrong-voice lines hold more than max_wrong_share of the words, or the captions matched fewer
             than min_match of the script's words.
splice_take  a bad take's voice rebuilt from good takes: every line taken from a take that has the same speaker and
             text and passed the check, consecutive lines from one take cut as one piece so the delivery stays natural.

Command line (for a day folder by hand):  python voice_check.py FOLDER [OTHER_TAKE_FOLDER ...] [--splice]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PITCH_FILES = [HERE / "voice-pitch.json", HERE / "voice-pitch-oldman.json"]
SR = 16000          # pitch is measured at 16 kHz mono
FRAME, HOP = 0.04, 0.01
F0_MIN, F0_MAX = 70.0, 400.0

DEFAULTS = {
    "min_match": 0.75,          # captions must match at least this share of the script's words
    "max_wrong_share": 0.15,    # at most this share of the words may be in another cast member's voice
    "margin_semitones": 2.0,    # a line counts as wrong only when it is this much nearer the other voice
    "min_voiced_frames": 8,     # lines with fewer voiced 10 ms frames ("Bilkul.") are not judged
    "min_gap_semitones": 3.0,   # cast voices closer than this can't be told apart by pitch: no voice test
}


# ---------- audio ----------

def load_audio(path: Path, sr: int = SR) -> np.ndarray:
    """Mono float32 at `sr` (ffmpeg decodes any format the pipeline writes)."""
    raw = subprocess.run(["ffmpeg", "-v", "quiet", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def frame_f0(x: np.ndarray, sr: int = SR) -> np.ndarray:
    """F0 per 10 ms frame (NaN where unvoiced), by normalised autocorrelation computed with an FFT."""
    n, hop = int(FRAME * sr), int(HOP * sr)
    if len(x) < n:
        return np.array([])
    count = 1 + (len(x) - n) // hop
    idx = np.arange(n)[None, :] + hop * np.arange(count)[:, None]
    fr = x[idx]
    fr = fr - fr.mean(axis=1, keepdims=True)
    rms = np.sqrt((fr ** 2).mean(axis=1))
    spec = np.fft.rfft(fr * np.hanning(n), n=2 * n, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, axis=1)[:, :n]
    lo, hi = int(sr / F0_MAX), int(sr / F0_MIN)
    seg = ac[:, lo:hi]
    k = seg.argmax(axis=1)
    peak = seg[np.arange(count), k] / np.maximum(ac[:, 0], 1e-12)
    loud = rms > max(1e-4, 0.1 * np.percentile(rms, 95))
    f0 = sr / (lo + k)
    f0[(peak < 0.45) | ~loud] = np.nan
    return f0


def line_pitches(f0: np.ndarray, lines: list[dict]) -> list[tuple[float | None, int]]:
    """(median F0, voiced frames) for each timeline line."""
    out = []
    for ln in lines:
        a, b = int(float(ln["start"]) / HOP), int(math.ceil(float(ln["end"]) / HOP))
        v = f0[max(a, 0):max(b, 0)]
        v = v[~np.isnan(v)]
        out.append((float(np.median(v)) if len(v) else None, int(len(v))))
    return out


# ---------- cast pitch ----------

def voice_pitch() -> dict[str, float]:
    """voice name -> measured median Hz (voice-pitch.json, then the old-man audition)."""
    out: dict[str, float] = {}
    for f in PITCH_FILES:
        if f.is_file():
            for name, d in json.loads(f.read_text(encoding="utf-8")).items():
                out.setdefault(name, float(d["median_hz"]))
    return out


def cast_pitch(voice_json: dict, pitches: dict[str, float] | None = None) -> dict[str, float]:
    """speaker id -> Hz, from voice.json's "voices" ({speaker: {"voice": name}} or {speaker: name}). Unknown voices
    are left out."""
    pitches = voice_pitch() if pitches is None else pitches
    out = {}
    for spk, v in (voice_json.get("voices") or {}).items():
        name = v.get("voice") if isinstance(v, dict) else v
        if name in pitches:
            out[spk] = pitches[name]
    return out


def semitones(a: float, b: float) -> float:
    return 12.0 * math.log2(a / b)


# ---------- the check ----------

def words(text: str) -> int:
    return len(re.findall(r"[\w']+", text))


def check_take(script: dict, timeline: dict, pitches: list[tuple[float | None, int]], cast: dict[str, float],
               settings: dict | None = None) -> dict:
    """{"ok", "match", "wrong_share", "lines": [...], "reasons": [...]} for one take."""
    s = {**DEFAULTS, **(settings or {})}
    match = float(timeline.get("match") or 0.0)
    reasons = []
    if match < s["min_match"]:
        reasons.append(f"captions matched only {match:.0%} of the words (need {s['min_match']:.0%})")
    speakers = sorted({ln["speaker"] for ln in script["lines"]})
    known = [spk for spk in speakers if spk in cast]
    gaps = [abs(semitones(cast[a], cast[b])) for i, a in enumerate(known) for b in known[i + 1:]]
    testable = len(known) >= 2 and len(known) == len(speakers) and min(gaps) >= s["min_gap_semitones"]
    rows, wrong_words, total_words = [], 0, 0
    for i, ln in enumerate(script["lines"]):
        hz, voiced = pitches[i] if i < len(pitches) else (None, 0)
        w = words(ln["text"])
        total_words += w
        row = {"line": i + 1, "speaker": ln["speaker"], "hz": round(hz, 1) if hz else None, "voiced": voiced, "verdict": "ok"}
        if not testable:
            row["verdict"] = "not tested"
        elif hz is None or voiced < s["min_voiced_frames"]:
            row["verdict"] = "too short"
        else:
            own = abs(semitones(hz, cast[ln["speaker"]]))
            near_spk, near = min(((o, abs(semitones(hz, cast[o]))) for o in known if o != ln["speaker"]), key=lambda t: t[1])
            if near + s["margin_semitones"] <= own:
                row["verdict"] = f"sounds like {near_spk}"
                wrong_words += w
        rows.append(row)
    share = wrong_words / total_words if total_words else 0.0
    if share > s["max_wrong_share"]:
        bad = [f"{r['line']} ({r['speaker']}, {r['hz']:.0f} Hz)" for r in rows if r["verdict"].startswith("sounds like")]
        reasons.append(f"{share:.0%} of the words are in another cast voice: lines {', '.join(bad)}")
    if not testable and len(speakers) >= 2:
        reasons_note = "voice test skipped (cast voices too close in pitch or not measured)"
    else:
        reasons_note = ""
    return {"ok": not reasons, "match": round(match, 3), "wrong_share": round(share, 3), "voice_tested": testable,
            "note": reasons_note, "reasons": reasons, "lines": rows}


def check_folder(folder: Path, settings: dict | None = None, pitches_hz: dict[str, float] | None = None) -> dict:
    """check_take for a take folder (script.json, timeline.json, voice.wav, voice.json); writes voice-check.json."""
    script = json.loads((folder / "script.json").read_text(encoding="utf-8"))
    timeline = json.loads((folder / "timeline.json").read_text(encoding="utf-8"))
    vj = json.loads((folder / "voice.json").read_text(encoding="utf-8")) if (folder / "voice.json").is_file() else {}
    cast = cast_pitch(vj, pitches_hz)
    f0 = frame_f0(load_audio(folder / "voice.wav"))
    res = check_take(script, timeline, line_pitches(f0, timeline.get("lines", [])), cast, settings)
    (folder / "voice-check.json").write_text(json.dumps(res, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return res


def summary(res: dict) -> str:
    if res["ok"]:
        return f"voice check ok (captions {res['match']:.0%}" + (", voices right)" if res.get("voice_tested") else ", voice test skipped)")
    return "voice check FAILED: " + "; ".join(res["reasons"])


# ---------- the splice ----------

def norm(text: str) -> str:
    return " ".join(re.findall(r"[\w']+", text.lower()))


def plan_splice(script: dict, takes: list[dict]) -> list[tuple[int, int]] | None:
    """For each script line: (take index, line index in that take) from a take with the same speaker and text whose
    line passed the check, preferring to stay in the take the previous line came from. None if a line has no source."""
    plan: list[tuple[int, int]] = []
    for i, ln in enumerate(script["lines"]):
        key = (ln["speaker"], norm(ln["text"]))
        options = []
        for t, take in enumerate(takes):
            for j, other in enumerate(take["script"]["lines"]):
                if (other["speaker"], norm(other["text"])) == key and not take["check"]["lines"][j]["verdict"].startswith("sounds like"):
                    options.append((t, j))
        if not options:
            return None
        prev = plan[-1] if plan else None
        best = next((o for o in options if prev and o == (prev[0], prev[1] + 1)), None)
        best = best or next((o for o in options if o[1] == i), None) or options[0]
        plan.append(best)
    return plan


def splice_take(script: dict, takes: list[dict], out_wav: Path, pad: float = 0.15) -> list[dict] | None:
    """Write out_wav from the takes ({"script", "timeline", "check", "wav"}); returns the pieces, or None when a line
    has no good source. Each piece runs from just before its first line to just after its last one (half the gap to
    the neighbouring line, at most `pad`), so the pauses between lines stay close to the original ones."""
    plan = plan_splice(script, takes)
    if plan is None:
        return None
    groups: list[list[tuple[int, int]]] = []
    for p in plan:
        if groups and groups[-1][-1][0] == p[0] and groups[-1][-1][1] + 1 == p[1]:
            groups[-1].append(p)
        else:
            groups.append([p])
    audio: dict[int, tuple[np.ndarray, int]] = {}
    pieces, chunks = [], []
    for g in groups:
        t = g[0][0]
        take = takes[t]
        if t not in audio:
            with wave.open(str(take["wav"]), "rb") as w:
                sr, ch = w.getframerate(), w.getnchannels()
                x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).reshape(-1, ch)
            audio[t] = (x, sr)
        x, sr = audio[t]
        tl = take["timeline"]["lines"]
        first, last = g[0][1], g[-1][1]
        dur = len(x) / sr
        before = float(tl[first - 1]["end"]) if first > 0 else None
        after = float(tl[last + 1]["start"]) if last + 1 < len(tl) else None
        a = float(tl[first]["start"]) - (min(pad, (float(tl[first]["start"]) - before) / 2) if before is not None else pad)
        b = float(tl[last]["end"]) + (min(pad, (after - float(tl[last]["end"])) / 2) if after is not None else dur)
        a, b = max(0.0, a), min(dur, b)
        chunks.append(x[int(a * sr):int(b * sr)])
        pieces.append({"take": take.get("name", str(t)), "lines": [first + 1, last + 1], "from": round(a, 3), "to": round(b, 3)})
    rates = {audio[t][1] for t in audio}
    chans = {audio[t][0].shape[1] for t in audio}
    if len(rates) != 1 or len(chans) != 1:
        raise ValueError(f"takes differ in format (rates {rates}, channels {chans})")
    out = np.concatenate(chunks)
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(chans.pop()); w.setsampwidth(2); w.setframerate(rates.pop())
        w.writeframes(out.astype(np.int16).tobytes())
    return pieces


def take_info(folder: Path, name: str, settings: dict | None = None) -> dict:
    res = check_folder(folder, settings) if not (folder / "voice-check.json").is_file() else json.loads((folder / "voice-check.json").read_text(encoding="utf-8"))
    return {"name": name, "folder": folder, "wav": folder / "voice.wav", "check": res,
            "script": json.loads((folder / "script.json").read_text(encoding="utf-8")),
            "timeline": json.loads((folder / "timeline.json").read_text(encoding="utf-8"))}


def main() -> None:
    p = argparse.ArgumentParser(description="Check a voice take; optionally rebuild it from other takes.")
    p.add_argument("folder", type=Path)
    p.add_argument("others", type=Path, nargs="*", help="other take folders (same day) to splice from")
    p.add_argument("--splice", action="store_true", help="write FOLDER/voice.wav from the good takes (old one kept as voice.bad.wav)")
    a = p.parse_args()
    res = check_folder(a.folder)
    print(summary(res))
    for r in res["lines"]:
        print(f"  {r['line']:2} {r['speaker']:8} {str(r['hz'] or '-'):>6} Hz  {r['verdict']}")
    if a.splice:
        others = [take_info(f, f.name) for f in a.others]
        for o in others:
            print(f"{o['name']}: {summary(o['check'])}")
        good = [o for o in others if o["check"]["ok"]]
        script = json.loads((a.folder / "script.json").read_text(encoding="utf-8"))
        bad = a.folder / "voice.bad.wav"
        if not bad.exists():
            (a.folder / "voice.wav").replace(bad)
        pieces = splice_take(script, good, a.folder / "voice.wav")
        if pieces is None:
            bad.replace(a.folder / "voice.wav")
            sys.exit("no good take has every line")
        print("spliced:", json.dumps(pieces))


if __name__ == "__main__":
    main()
