"""run.py: the script formats, slots, settings and the YouTube 'subscribe' variant."""

from __future__ import annotations

import pytest


def test_split_sections(run_py):
    s = run_py.split_sections("a\nb\n--- description\nd1\n--- trial\nt1\n--- trial description\ntd")
    assert s == {"body": "a\nb", "description": "d1", "trial": "t1", "trial description": "td"}


@pytest.mark.parametrize("title, spoken", [
    ("QA Engineer I, Music Client QA", "QA Engineer One"),
    ("Data Analyst (Remote)", "Data Analyst"),
    ("Backend Engineer - Payments", "Backend Engineer"),
])
def test_spoken_title(run_py, title, spoken):
    assert run_py.spoken_title(title) == spoken


def test_fill_slots(run_py):
    jobs = [{"company": "Infosys", "title": "Data Analyst II", "where": "Pune, India", "posted": "today", "apply_url": "https://x/1"}]
    assert run_py.fill_slots("Job: {job1}.", jobs) == "Job: Infosys, Data Analyst Two, Pune."
    assert run_py.fill_slots("{job1.company} in {job1.city}: {job1.url}", jobs) == "Infosys in Pune: https://x/1"


def test_with_platform_link_goes_above_hashtags(run_py):
    out = run_py.with_platform_link("Body\n\n#a #b", {"name": "n8n", "url": "https://n8n.io/x"})
    assert out.splitlines()[-1] == "#a #b" and "N8n: https://n8n.io/x" in out
    assert run_py.with_platform_link("Body https://n8n.io/x", {"url": "https://n8n.io/x"}) == "Body https://n8n.io/x"


def test_for_youtube(run_py):
    lines = [{"text": "Follow-up mail bhejo."}, {"text": "Theek hai."}, {"text": "Follow karo, kal salary."}]
    yt = run_py.for_youtube(lines)
    assert yt[-1]["text"] == "Subscribe karo, kal salary."
    assert yt[0]["text"] == "Follow-up mail bhejo."  # only the last two lines, and never "follow-up"
    assert run_py.for_youtube([{"text": "a"}, {"text": "b"}]) is None


def test_parse_script(run_py, tmp_path):
    chars = run_py.characters()
    f = tmp_path / "s.txt"
    f.write_text("hook: ZERO CALLS\n# a note\nRiya: Ma'am? [show: https://example.com]\n"
                 "Malhotra Ma'am (shocked): Kyun? [job: 1]\n", encoding="utf-8")
    s = run_py.parse_script(f, chars)
    assert s["hook_card"] == "ZERO CALLS"
    assert [ln["speaker"] for ln in s["lines"]] == ["riya", "malhotra"]
    assert s["lines"][0]["show"] == "https://example.com" and s["lines"][0]["text"] == "Ma'am?"
    assert s["lines"][1]["expression"] == "shocked" and s["lines"][1]["job_ref"] == 1


def test_parse_script_rejects_unknown_speaker(run_py, tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("Nobody: hi\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="unknown character"):
        run_py.parse_script(f, run_py.characters())


def test_parse_sets(run_py):
    changes, lines = run_py.parse_sets(["compose.panel.scroll=120", "run.visuals=false"], {"visuals": True})
    assert changes == {"visuals": False}
    assert lines == ['"compose"."panel"."scroll" = 120']
    with pytest.raises(SystemExit, match="not in tools/composer/config.toml"):
        run_py.parse_sets(["compose.panel.scrol=120"], {})
    with pytest.raises(SystemExit):
        run_py.parse_sets(["nonsense"], {})


def test_script_from_text(run_py, tmp_path, monkeypatch):
    monkeypatch.setattr(run_py, "ROOT", tmp_path)
    p = run_py.script_from_text("hook: X | Riya: a | Arjun: b", "t")
    assert p.read_text(encoding="utf-8") == "hook: X\nRiya: a\nArjun: b\n"
