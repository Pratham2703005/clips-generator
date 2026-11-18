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


def test_dm_stats_sums_days(monkeypatch):
    import io
    import json as _json
    per_day = {"2026-11-17": {"dms": 3, "tgClicks": 2, "followClicks": 1, "tgJoins": 1, "followsYes": 1, "followsNo": 1, "followsUnknown": 1},
               "2026-11-12": {"dms": 1, "tgClicks": 0, "followClicks": 0, "tgJoins": 0, "followsYes": 0, "followsNo": 0, "followsUnknown": 0},
               "2026-11-18": {"dms": 5, "tgClicks": 0, "followClicks": 0, "tgJoins": 0, "followsYes": 0, "followsNo": 0, "followsUnknown": 0}}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(rp.urllib.request, "urlopen", lambda req, timeout=30: Resp(_json.dumps(per_day).encode()))
    s = rp.dm_stats({"stats_url": "https://x/stats"}, "2026-11-18")
    assert s["yesterday"]["dms"] == 3 and s["week"]["dms"] == 4 and s["all"]["dms"] == 9
    line = rp.dm_line("Yesterday", s["yesterday"])
    assert line.startswith("Yesterday: 3 DMs sent · 2 Telegram button clicks") and "1 yes, 1 no, 1 unknown" in line
    assert rp.dm_stats({}, "2026-11-18") == {}
