"""Every hand-written episode in series/ parses the way the nightly run reads it, and keeps the content rules."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from conftest import ROOT

EPISODES = sorted((ROOT / "series").glob("*.txt"))
FAKE_JOBS = [{"id": f"j{n}", "company": f"Company{n}", "title": f"Data Analyst {n}", "where": "Bengaluru, India",
              "posted": "today", "apply_url": f"https://example.com/apply/{n}"} for n in (1, 2, 3)]
# The Interview prep reels (2026-11-15 on) promote at most 3 of every 10 (user, 2026-11-07); older seasons were planned
# before that rule.
PROMO_RULE_FROM = "2026-11-15"


def test_there_are_episodes():
    assert EPISODES, "no series/*.txt"


@pytest.mark.parametrize("path", EPISODES, ids=lambda p: p.stem)
def test_episode_parses_and_fills(path: Path, run_py, tmp_path):
    ep = run_py.parse_series(path)
    assert ep["title"].strip(), "no title:"
    assert len(ep["cast"]) == 2, f"cast must name two characters, got {ep['cast']}"
    chars = run_py.characters()
    for c in ep["cast"]:
        assert run_py.find_char(c, chars), f"unknown character {c!r}"

    # header lines never reach the spoken dialogue (a new header field must be added to parse_series)
    for line in ep["dialogue"].splitlines():
        assert not re.match(r"^\s*(title|cast|jobs|funnel|trial_hook|trial_card|yt_last|threads)\s*:", line, re.I), line

    slots = {int(n) for n in re.findall(r"\{job(\d)", ep["dialogue"])}
    assert not slots or max(slots) <= max(ep["count"], 1), f"dialogue uses {{job{max(slots)}}} but jobs count is {ep['count']}"

    # the filled dialogue is a valid script: every line 'Name: text', known speakers
    filled = tmp_path / "s.txt"
    filled.write_text(run_py.fill_slots(ep["dialogue"], FAKE_JOBS), encoding="utf-8")
    try:
        parsed = run_py.parse_script(filled, chars)
    except SystemExit as e:
        pytest.fail(str(e))
    assert parsed["lines"], "no spoken lines"
    assert "{job" not in filled.read_text(encoding="utf-8"), "an unfilled slot is left"


@pytest.mark.parametrize("path", EPISODES, ids=lambda p: p.stem)
def test_value_episodes_promote_nothing(path: Path, run_py):
    ep = run_py.parse_series(path)
    if ep["count"] != 0:
        return
    assert "{job" not in ep["dialogue"], "a value episode (jobs: ..., 0) has a job slot"
    assert not re.search(r"link in bio", ep["description"], re.I), "a value episode's description points to the bio link"


@pytest.mark.parametrize("path", EPISODES, ids=lambda p: p.stem)
def test_variant_lines(path: Path, run_py):
    ep = run_py.parse_series(path)
    assert len(ep["threads"]) <= 500, "threads: is longer than Threads' 500 characters"
    if ep["trial_hook"]:
        assert ep["trial_card"], "trial_hook without trial_card"
    assert ep["description"], "no '--- description' part"


def test_promo_ratio():
    """At most 3 promo episodes in any 10 consecutive days of the Interview prep reels."""
    dated = [(p.stem, p) for p in EPISODES if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem) and p.stem >= PROMO_RULE_FROM]
    promo = []
    for day, p in dated:
        text = p.read_text(encoding="utf-8")
        m = re.search(r"(?mi)^\s*jobs\s*:\s*[^,\n]*,\s*(\d+)", text)
        promo.append((day, bool(m and int(m.group(1)) > 0)))
    for i in range(max(0, len(promo) - 9)):
        window = promo[i:i + 10]
        n = sum(1 for _, is_promo in window if is_promo)
        assert n <= 3, f"{n} promo episodes between {window[0][0]} and {window[-1][0]}"


FUNNEL_FROM = "2026-11-14"  # every episode from here has one funnel job (2026-11-13)


def dated_from(day: str) -> list[Path]:
    return [p for p in EPISODES if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem) and p.stem >= day]


@pytest.mark.parametrize("path", dated_from(FUNNEL_FROM), ids=lambda p: p.stem)
def test_funnel_role(path: Path, run_py):
    """top = reach, middle = follow, bottom = join the owned audience (jobs / Telegram). A promo episode is the bottom
    of the funnel and a bottom episode promotes, so the two can't drift apart."""
    ep = run_py.parse_series(path)
    assert ep["funnel"] in ("top", "middle", "bottom"), f"funnel: must be top, middle or bottom (got {ep['funnel']!r})"
    promo = ep["count"] > 0 or "{job" in ep["dialogue"]
    assert promo == (ep["funnel"] == "bottom"), "a promo episode must be funnel: bottom, and a bottom one must promote"


def test_funnel_mix():
    """Reach first: in any 10 consecutive episodes at least 4 are top and at most 3 are bottom."""
    roles = [(p.stem, (re.search(r"(?mi)^\s*funnel\s*:\s*(\w+)", p.read_text(encoding="utf-8")) or [None, ""])[1].lower())
             for p in dated_from(FUNNEL_FROM)]
    for i in range(max(0, len(roles) - 9)):
        window = [r for _, r in roles[i:i + 10]]
        span = f"{roles[i][0]}..{roles[min(i + 9, len(roles) - 1)][0]}"
        assert window.count("top") >= 4, f"only {window.count('top')} top episodes in {span}"
        assert window.count("bottom") <= 3, f"{window.count('bottom')} bottom episodes in {span}"


def test_dates_are_real():
    for p in EPISODES:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem):
            date.fromisoformat(p.stem)
