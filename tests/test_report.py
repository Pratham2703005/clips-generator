"""report.py helpers: a refused metric is named in the mail, never breaks the report."""

from __future__ import annotations

from conftest import load

rp = load("tools/report/report.py")


def test_tolerant_all_at_once():
    values, refused = rp.tolerant(lambda ms: {m: 1 for m in ms}, ["views", "reach"])
    assert values == {"views": 1, "reach": 1} and refused == {}


def test_tolerant_one_refused():
    def fetch(ms):
        if "follows" in ms:
            raise RuntimeError("HTTP 400: does not support the follows metric")
        return {m: 5 for m in ms}
    values, refused = rp.tolerant(fetch, ["views", "follows", "saved"])
    assert values == {"views": 5, "saved": 5}
    assert list(refused) == ["follows"]
    assert "follows" in rp.refused_note("Instagram", refused)
    assert rp.refused_note("Instagram", {}) == ""


def test_tolerant_nothing_asked():
    assert rp.tolerant(lambda ms: 1 / 0, []) == ({}, {})


def test_metric_text():
    assert rp.metric_text("ig_reels_avg_watch_time", 17000) == "17.0 s"
    assert rp.metric_text("averageViewPercentage", 74.4) == "74%"
    assert rp.metric_text("views", None) == "-"
    assert rp.metric_text("views", 26336) == "26,336"


def test_account_line():
    line = rp.account_line("Instagram", 7, {"views": 100, "reach": None},
                           {"views": {"non_follower": 91, "follower": 9}})
    assert line.startswith("Instagram, last 7 days: 100 ") and "91% from non-followers" in line
    assert rp.account_line("Instagram", 7, {"views": None}) == ""
