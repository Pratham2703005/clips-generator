"""The 2026-11-22 reel review fixes: the hook tracker and count-up only for counts, the step list, visual picks for series
reels. Nothing here renders or opens Chrome."""

from __future__ import annotations

from conftest import load

vc = load("tools/visual-capture/capture.py")
cp = load("tools/composer/compose.py")


def test_tracker_only_for_application_counts():
    assert vc.TRACKER_HOOK.search("200 APPLICATIONS. 0 CALLS.")
    assert vc.TRACKER_HOOK.search("Maine 50 jobs pe apply kiya")
    # 2026-11-22: "10 MINUTE" made a 10-row tracker that scrolled into a blank white box
    assert not vc.TRACKER_HOOK.search("10 MINUTE RESUME FIX Ek resume, pachaas jobs. Reply zero.")


def test_series_cta_shows_no_developer_page():
    cfg = vc.load_config(vc.DEFAULT_CONFIG)
    script = {"track": "series", "lines": [{"text": "Hi"}, {"text": "Follow karke RESUME comment karo"}]}
    brief = {"platform": {"id": "make"}}
    assert vc.plan(script, brief, cfg) == []
    assert [v["key"] for v in vc.plan({**script, "track": "product_pitch"}, brief, cfg)] == ["make"]


def timeline(*texts):
    out, t = [], 0.0
    for text in texts:
        words = [{"word": w, "start": t + i * 0.3} for i, w in enumerate(text.split())]
        out.append({"text": text, "start": t, "end": t + 0.3 * len(words), "words": words})
        t += 0.3 * len(words) + 0.2
    return {"lines": out}


def test_steps_from_spoken_count_use_description_labels():
    tl = timeline("Teen step hain.", "Ek: skills dhoondo.", "Yahi galti hai. Do: unhe upar laao.", "Teen: summary likho.")
    caption = "Fix:\n1. JD ki 3 skills dhoondo\n2. Unhe upar laao\n3. Summary: role + skills\nJo nahi aata, mat likho."
    steps = cp.plan_steps({"caption": caption}, tl)
    assert [s[1] for s in steps] == ["JD ki 3 skills dhoondo", "Unhe upar laao", "Summary: role + skills"]
    # "Do:" lands on its own word, not at the start of its line
    do_line = tl["lines"][2]
    assert steps[1][0] == do_line["words"][3]["start"] > do_line["start"]


def test_steps_fall_back_to_spoken_words_and_need_a_run():
    tl = timeline("Ek: skills dhoondo.", "Do: upar laao. Phir dekho.")
    assert [s[1] for s in cp.plan_steps({}, tl)] == ["Skills dhoondo", "Upar laao"]
    assert cp.plan_steps({}, timeline("Do: upar laao.", "Teen: summary.")) == []  # doesn't start at 1
    assert cp.plan_steps({}, timeline("Ek resume, pachaas jobs.")) == []


def test_episode_steps_win_over_the_description():
    tl = timeline("Ek: job description mein skills dhoondo.", "Do: unhe upar laao.")
    caption = "1. Job description mein baar-baar aane wali 3 skills dhoondo\n2. Jo tumhare paas hain, unhe resume ke upar laao"
    assert [s[1] for s in cp.plan_steps({"caption": caption, "steps": ["JD ki 3 skills dhoondo", "Unhe upar laao"]}, tl)] \
        == ["JD ki 3 skills dhoondo", "Unhe upar laao"]


def test_series_steps_header(run_py, tmp_path):
    ep = tmp_path / "ep.txt"
    ep.write_text("title: T\ncast: arjun, riya\njobs: data-analyst-freshers, 0\nsteps: A | B c | D\nArjun: Ek: a. Do: b.\n", encoding="utf-8")
    parsed = run_py.parse_series(ep)
    assert parsed["steps"] == ["A", "B c", "D"] and "steps" not in parsed["dialogue"]


pc = load("tools/content-data-preparation/prepare_content.py")


def test_job_levels_and_acronyms():
    for title in ("Vlsi Engineer L2", "Technical Program Manager II", "Associate Manager, Finance", "Analyst Level 3"):
        assert pc.too_senior({"title": title}), title
    for title in ("Data Analyst", "Business Analyst Intern", "Associate Software Engineer", "Graduate Engineer Trainee"):
        assert not pc.too_senior({"title": title}), title
    assert pc.clean_title("Vlsi Engineer L2") == "VLSI Engineer L2"
    assert pc.clean_title("Power Bi Developer - Sql") == "Power BI Developer - SQL"


def test_free_spans():
    assert cp.free_spans(0, 10, [(2, 3), (8, 12)]) == [(0, 2), (3, 8)]
    assert cp.free_spans(0, 10, []) == [(0, 10)]


def test_hook_count_up_only_for_counts():
    cfg = cp.load_config(cp.DEFAULT_CONFIG)
    tl = timeline("Ek resume, pachaas jobs.")
    ass, _ = cp.make_overlay({"hook_card": "10 MINUTE RESUME FIX", "lines": [{"text": tl["lines"][0]["text"]}]}, tl, cfg)
    hook = [ln for ln in ass.splitlines() if ",hook," in ln]
    assert len(hook) == 1 and "10 MINUTE RESUME FIX" in hook[0]  # the whole card at once, no "1", "2", ...
    ass, _ = cp.make_overlay({"hook_card": "200 APPLICATIONS. 0 CALLS.", "lines": [{"text": tl["lines"][0]["text"]}]}, tl, cfg)
    assert len([ln for ln in ass.splitlines() if ",hook," in ln]) > 2
