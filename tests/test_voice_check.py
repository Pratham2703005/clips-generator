"""Voice check + repair (2026-11-27: the 27 Nov reel had Arjun's lines in Riya's voice and was posted anyway).

Synthetic takes: each line is a 1 s harmonic tone at its speaker's pitch (Arjun ~115 Hz, Riya ~255 Hz) with 0.3 s of
silence between lines, so a timeline is known exactly. Needs ffmpeg (the check decodes audio with it)."""

from __future__ import annotations

import json
import shutil
import wave

import numpy as np
import pytest

from conftest import load

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

SR = 48000
LINE, GAP = 1.0, 0.3
VOICES = {"arjun": {"label": "Arjun", "voice": "Algenib"}, "riya": {"label": "Riya", "voice": "Leda"}}
PITCH = {"Algenib": 113.5, "Leda": 258.0}
HZ = {"arjun": 115.0, "riya": 255.0}


@pytest.fixture
def vc():
    return load("tools/voice-generator/voice_check.py")


def tone(hz: float, seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    x = sum(np.sin(2 * np.pi * hz * k * t) / k for k in range(1, 6))
    return (0.3 * x / np.abs(x).max() * 32767).astype(np.int16)


def make_take(folder, lines, spoken_by=None, match=0.95):
    """A take folder: script.json, voice.json, voice.wav, timeline.json. spoken_by[i] = whose voice line i really has."""
    folder.mkdir(parents=True, exist_ok=True)
    spoken_by = spoken_by or [ln[0] for ln in lines]
    audio, tl, t = [], [], 0.0
    for (spk, text), voice in zip(lines, spoken_by):
        audio += [tone(HZ[voice], LINE), np.zeros(int(GAP * SR), dtype=np.int16)]
        tl.append({"speaker": spk, "text": text, "start": round(t, 3), "end": round(t + LINE, 3)})
        t += LINE + GAP
    with wave.open(str(folder / "voice.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(np.concatenate(audio).tobytes())
    (folder / "script.json").write_text(json.dumps({"lines": [{"speaker": s, "text": x} for s, x in lines]}), encoding="utf-8")
    (folder / "voice.json").write_text(json.dumps({"voices": VOICES}), encoding="utf-8")
    (folder / "timeline.json").write_text(json.dumps({"match": match, "lines": tl}), encoding="utf-8")
    return folder


SCRIPT = [("riya", "Saath rejection ho gaye."), ("arjun", "Saath rejection ek jaise nahi hote."),
          ("riya", "Rejection toh rejection hai."), ("arjun", "Nahi, dikkat resume mein hai."),
          ("arjun", "Follow karke SABAR comment karo.")]


def test_good_take_passes(vc, tmp_path):
    res = vc.check_folder(make_take(tmp_path / "t", SCRIPT), pitches_hz=PITCH)
    assert res["ok"] and res["voice_tested"]
    assert [r["verdict"] for r in res["lines"]] == ["ok"] * 5
    assert json.loads((tmp_path / "t" / "voice-check.json").read_text())["ok"]


def test_arjun_in_riyas_voice_fails(vc, tmp_path):
    # the 27 Nov failure: Arjun's lines spoken by the female voice
    res = vc.check_folder(make_take(tmp_path / "t", SCRIPT, spoken_by=["riya", "riya", "riya", "riya", "arjun"]), pitches_hz=PITCH)
    assert not res["ok"]
    assert [r["verdict"] for r in res["lines"]][1] == "sounds like riya"
    assert "another cast voice" in res["reasons"][0]


def test_low_caption_match_fails(vc, tmp_path):
    res = vc.check_folder(make_take(tmp_path / "t", SCRIPT, match=0.62), pitches_hz=PITCH)
    assert not res["ok"] and "62%" in res["reasons"][0]


def test_one_short_wrong_line_is_tolerated(vc, tmp_path):
    # 1 wrong line of 12 short lines is under max_wrong_share (15% of the words)
    lines = SCRIPT * 2 + [("riya", "Haan."), ("arjun", "Bilkul.")]
    voices = [s for s, _ in lines]
    voices[-1] = "riya"
    res = vc.check_folder(make_take(tmp_path / "t", lines, spoken_by=voices), pitches_hz=PITCH)
    assert res["ok"] and res["lines"][-1]["verdict"] == "sounds like riya"


def test_voices_too_close_are_not_tested(vc, tmp_path):
    res = vc.check_folder(make_take(tmp_path / "t", SCRIPT, spoken_by=["riya"] * 5), pitches_hz={"Algenib": 250.0, "Leda": 258.0})
    assert res["ok"] and not res["voice_tested"]


def test_splice_takes_lines_from_good_takes(vc, tmp_path):
    youtube_lines = SCRIPT[:-1] + [("arjun", "Subscribe karo.")]
    trial_lines = [("riya", "Sixty rejections is data.")] + SCRIPT[1:]
    yt = make_take(tmp_path / "youtube", youtube_lines)
    tr = make_take(tmp_path / "trial", trial_lines)
    takes = [vc.take_info(yt, "youtube"), vc.take_info(tr, "trial")]
    for tk in takes:
        tk["check"] = vc.check_folder(tk["folder"], pitches_hz=PITCH)
    out = tmp_path / "voice.wav"
    pieces = vc.splice_take({"lines": [{"speaker": s, "text": x} for s, x in SCRIPT]}, takes, out)
    assert [(p["take"], p["lines"]) for p in pieces] == [("youtube", [1, 4]), ("trial", [5, 5])]
    with wave.open(str(out)) as w:
        seconds = w.getnframes() / w.getframerate()
    assert seconds == pytest.approx(4 * (LINE + GAP) - GAP + 0.15 + 0.15 + LINE + GAP, abs=0.02)


def test_splice_needs_every_line(vc, tmp_path):
    other = make_take(tmp_path / "o", SCRIPT[:-1] + [("arjun", "Subscribe karo.")])
    tk = vc.take_info(other, "o")
    tk["check"] = vc.check_folder(other, pitches_hz=PITCH)
    assert vc.splice_take({"lines": [{"speaker": s, "text": x} for s, x in SCRIPT]}, [tk], tmp_path / "v.wav") is None


def test_splice_skips_wrong_voice_lines(vc, tmp_path):
    a = make_take(tmp_path / "a", SCRIPT, spoken_by=["riya", "riya", "riya", "arjun", "arjun"])  # line 2 bad
    b = make_take(tmp_path / "b", SCRIPT)
    takes = [vc.take_info(a, "a"), vc.take_info(b, "b")]
    for tk in takes:
        tk["check"] = vc.check_folder(tk["folder"], pitches_hz=PITCH)
    plan = vc.plan_splice({"lines": [{"speaker": s, "text": x} for s, x in SCRIPT]}, takes)
    assert plan[1] == (1, 1)  # line 2 only from take b


# ---------- run.py: repair_takes ----------

@pytest.fixture
def day(tmp_path, vc, run_py, monkeypatch):
    """A day folder whose reel take is bad, plus good trial + youtube takes; captions/compose faked."""
    monkeypatch.setattr(vc, "voice_pitch", lambda: dict(PITCH))
    monkeypatch.setattr(run_py.voice_check_module(), "voice_pitch", lambda: dict(PITCH))  # run.py's own import
    folder = make_take(tmp_path / "2026-11-27", SCRIPT, spoken_by=["riya", "riya", "riya", "riya", "arjun"], match=0.62)
    (folder / "compose.json").write_text(json.dumps({"background": {"clip": "clip3.mp4", "offset": 1473.49}}), encoding="utf-8")
    make_take(folder / "variants" / "trial", [("riya", "Sixty rejections is data.")] + SCRIPT[1:])
    make_take(folder / "variants" / "youtube", SCRIPT[:-1] + [("arjun", "Subscribe karo.")])
    return folder


def fake_steps(monkeypatch, run_py, calls):
    def fake_run(step, args, env):
        calls.append((step, args))
        if step == "captions":  # the rebuilt voice has the same layout as a clean take: an exact timeline
            f = __import__("pathlib").Path(args[1]).parent
            lines = json.loads((f / "script.json").read_text())["lines"]
            tl = [{"speaker": l["speaker"], "text": l["text"], "start": round(i * (LINE + GAP), 3), "end": round(i * (LINE + GAP) + LINE, 3)}
                  for i, l in enumerate(lines)]
            (f / "timeline.json").write_text(json.dumps({"match": 0.93, "lines": tl}))
        if step == "compose":
            f = __import__("pathlib").Path(args[1])
            (f / "reel.mp4").write_bytes(b"new")
    monkeypatch.setattr(run_py, "run", fake_run)


def test_repair_rebuilds_the_reel_from_the_variants(run_py, day, monkeypatch):
    for f in (day, day / "variants" / "trial", day / "variants" / "youtube"):
        run_py.check_voice(f, f.name)
    calls = []
    fake_steps(monkeypatch, run_py, calls)
    assert run_py.repair_takes(day, ["trial", "youtube"], {}) is True
    res = json.loads((day / "voice-check.json").read_text())
    assert res["ok"] and [p["take"] for p in res["repaired_from"]] == ["youtube", "trial"]
    assert (day / "voice.bad.wav").is_file() and (day / "reel.mp4").read_bytes() == b"new"
    assert ("compose", ["--folder", str(day), "--background", "clip3.mp4@1473.49"]) in calls


def test_reel_held_back_when_no_good_take(run_py, day, monkeypatch):
    run_py.check_voice(day, "reel")
    calls = []
    fake_steps(monkeypatch, run_py, calls)
    assert run_py.repair_takes(day, [], {}) is False
    assert (day / "voice.wav").is_file() and not (day / "voice.bad.wav").exists()  # bad voice left as it was
    assert calls == []


def test_bad_variant_is_held_back(run_py, day, monkeypatch):
    # good reel, bad youtube take whose last line exists nowhere else: its reel is renamed so nothing posts it
    make_take(day, SCRIPT)
    yt = make_take(day / "variants" / "youtube", SCRIPT[:-1] + [("arjun", "Subscribe karo.")], match=0.22)
    (yt / "reel.mp4").write_bytes(b"old")
    for f in (day, yt):
        run_py.check_voice(f, f.name)
    fake_steps(monkeypatch, run_py, [])
    assert run_py.repair_takes(day, ["youtube"], {}) is True
    assert (yt / "reel.bad.mp4").is_file() and not (yt / "reel.mp4").exists()


def test_all_good_does_nothing(run_py, day, monkeypatch):
    make_take(day, SCRIPT)
    run_py.check_voice(day, "reel")
    calls = []
    fake_steps(monkeypatch, run_py, calls)
    assert run_py.repair_takes(day, [], {}) is True and calls == []


def test_report_line(day, vc):
    report = load("tools/report/report.py")
    vc.check_folder(day)
    line = report.voice_check_line(day)
    assert line["warn"] and "reel FAILED (not posted)" in line["line"]
