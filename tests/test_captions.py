"""caption-maker: the script's words get Whisper's times (or spread ones), and the .ass file is well formed.
Whisper itself is not run here (the pipeline check does that)."""

from __future__ import annotations

from conftest import load

cm = load("tools/caption-maker/make_captions.py")


def heard(words):
    return [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate(words)]


def test_align_exact():
    times, rate = cm.align(["Hello", "world,", "bro"], heard(["hello", "world", "bro"]), 2.0)
    assert rate == 1.0 and [t["start"] for t in times] == [0.0, 0.5, 1.0]


def test_align_fills_misheard_words_in_order():
    script = ["Malhotra", "Ma'am", "ne", "bola", "range"]
    times, rate = cm.align(script, heard(["mel", "hotra", "ne", "bola", "range"]), 3.0)
    assert 0.5 < rate < 1.0
    starts = [t["start"] for t in times]
    assert starts == sorted(starts), "times went backwards"
    assert all(t["end"] >= t["start"] for t in times)


def test_timeline_and_ass():
    script = {"lines": [{"speaker": "riya", "name": "Riya", "text": "Kya bolun? Range!"},
                        {"speaker": "arjun", "name": "Arjun", "text": "Range bolo, research wali."}]}
    words = "Kya bolun Range Range bolo research wali".split()
    timeline, rate = cm.build_timeline(script, heard(words), 4.0)
    assert rate == 1.0 and len(timeline["lines"]) == 2
    assert timeline["lines"][1]["start"] >= timeline["lines"][0]["end"]
    cfg = cm.load_config(cm.DEFAULT_CONFIG)
    ass = cm.make_ass(timeline, cfg)
    assert "[Events]" in ass and ass.count("Dialogue:") >= 4
    assert "Style: riya," in ass and "Style: arjun," in ass


def test_ass_time_and_colour():
    assert cm.ass_time(3661.25) == "1:01:01.25"
    assert cm.ass_color("#FFE600") == "&H0000E6FF"
