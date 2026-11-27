"""The report's dashboard (tools/report/dashboard.py, 2026-11-27): views at 24 h, the watch list, five numbers, charts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import load

NOW = datetime(2026, 11, 27, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def dash():
    return load("tools/report/dashboard.py")


def post(i: int, hours_old: float, views: int, saves=10, shares=5, watch_ms=12000, caption=None):
    return {"id": f"p{i}", "ts": NOW - timedelta(hours=hours_old), "views": views, "saved": saves, "shares": shares,
            "ig_reels_avg_watch_time": watch_ms, "media_product_type": "REELS",
            "caption": caption or f"Reel number {i} | search words", "permalink": f"https://instagram.com/reel/{i}"}


def snap(hours_ago: float, posts: dict, followers: int) -> dict:
    return {"at": (NOW - timedelta(hours=hours_ago)).isoformat(), "ig": {"followers": followers, "posts": posts}}


def test_views_at_24h_from_snapshots(dash):
    # p1 is 3 days old: its 24 h number comes from the snapshot taken 2 days ago (age then ~24 h), not today's total
    ig = {"posts": [post(1, 72, 5000), post(2, 24, 1800), post(3, 3, 40)]}
    history = [snap(48, {"p1": {"views": 2100, "saves": 9, "shares": 3}}, 20)]
    rows = dash.reels(ig, history, NOW)
    assert [(r["id"], r["v24"]) for r in rows] == [("p1", 2100), ("p2", 1800), ("p3", None)]
    assert rows[0]["ss24"] == 12 and rows[1]["ss24"] == 15
    assert rows[0]["title"] == "Reel number 1"


def daily(n=10, flop_last=False):
    posts, history = [], []
    for i in range(n):
        age = 24 * (n - i)
        v = 300 if (flop_last and i == n - 1) else 2000 + 100 * i
        posts.append(post(i, age, v * 2))
    for i in range(n - 1):  # a snapshot when each older reel was ~24 h old
        age_then = 24 * (n - i) - 24
        history.append(snap(age_then, {f"p{i}": {"views": 2000 + 100 * i, "saves": 20, "shares": 10}}, 20 + i))
    return {"posts": posts}, history


def watch(dash, tmp_path, ig, history, **kw):
    rows = dash.reels(ig, history, NOW)
    cur = {"ig": {"followers": kw.pop("followers", 40), "posts": {}}}
    args = dict(folder=tmp_path, day="2026-11-27", rows=rows, steps=kw.pop("steps", [("Reel ready", "success", "")]),
                tokens=kw.pop("tokens", []), voice_line=kw.pop("voice_line", {}), models=kw.pop("models", {}), cur=cur,
                history=history, ig=ig, yt=kw.pop("yt", {}), now=NOW, settings={}, series_dir=kw.pop("series_dir", tmp_path))
    return dash.watch_items(**args)


def test_flop_and_missing_episode_are_flagged(dash, tmp_path):
    ig, history = daily(flop_last=True)
    items, passed = watch(dash, tmp_path, ig, history)
    titles = [i["title"] for i in items]
    assert "Yesterday's reel flopped" in titles
    assert "No episode for 28 Nov" in titles


def test_quiet_night_is_all_fine(dash, tmp_path):
    ig, history = daily()
    (tmp_path / "2026-11-28.txt").write_text("title: x\n", encoding="utf-8")
    items, passed = watch(dash, tmp_path, ig, history, voice_line={"line": "Voice check: reel ok (93%)", "warn": False})
    assert [i for i in items if i["level"] in ("bad", "warn")] == []
    assert "voice ok" in passed and "posted" in passed and "tomorrow's episode ready" in passed


def test_failed_post_voice_and_quota_fallback(dash, tmp_path):
    ig, history = daily()
    items, _ = watch(dash, tmp_path, ig, history,
                     steps=[("Reel ready", "success", ""), ("YouTube Short", "failure", "")],
                     voice_line={"line": "Voice check: reel FAILED (not posted): captions 62%", "warn": True},
                     models={"voice": {"model": "gemini-3.8-flash-tts",
                                       "tried": [{"model": "gemini-2.5-flash-preview-tts", "outcome": "429 daily quota"},
                                                 {"model": "gemini-3.8-flash-tts", "outcome": "used (attempt 1)"}]}})
    levels = {i["title"]: i["level"] for i in items}
    assert levels["Not posted: YouTube Short"] == "bad"
    assert levels["Reel voice wrong, reel held back"] == "bad"
    assert levels["Backup voice model used"] == "warn"
    assert items[0]["level"] == "bad"  # worst first


def test_flat_followers(dash, tmp_path):
    ig, _ = daily()
    items, _ = watch(dash, tmp_path, ig, [snap(72, {}, 40)], followers=40)
    assert "Followers stuck at 40 for 3 days" in [i["title"] for i in items]
    items, passed = watch(dash, tmp_path, ig, [snap(72, {}, 35)], followers=40)
    assert not any(i["title"].startswith("Followers stuck") for i in items)
    assert "followers +5 in 3 days" in passed


def test_five_numbers_against_targets(dash):
    ig, history = daily()
    ig["account"] = {"values": {"reach": 9000, "profile_links_taps": 0}}
    rows = dash.reels(ig, history, NOW)
    cur = {"ig": {"followers": 30, "posts": {}}}
    nums = {m["key"]: m for m in dash.five_numbers(rows, history, cur, ig, {"week": {"dms": 4}}, NOW, dict(dash.TARGETS))}
    assert set(nums) == {"views_24h", "saves_shares_per_1k", "followers_per_1k", "watch_s", "own_channel_week"}
    assert nums["own_channel_week"]["now"] == 4 and nums["own_channel_week"]["status"] == "bad"
    assert nums["own_channel_week"]["action"]
    assert nums["watch_s"]["now"] == 12.0
    assert nums["views_24h"]["status"] in ("warn", "bad")  # ~2,700 < 3,000


def test_changes_file(dash, tmp_path):
    f = tmp_path / "changes.toml"
    f.write_text('[[change]]\ndate = 2026-11-27\nwhat = "A"\nwhy = "B"\nwatch = "C"\n'
                 '[[change]]\ndate = 2026-11-01\nwhat = "old"\nwhy = "x"\n', encoding="utf-8")
    got = dash.recent_changes(NOW, 3, f)
    assert [c["what"] for c in got] == ["A"] and got[0]["date"] == "2026-11-27"


def test_render_has_charts_and_verdict(dash, tmp_path):
    pytest.importorskip("matplotlib")
    ig, history = daily(flop_last=True)
    ig["account"] = {"values": {"reach": 9000, "accounts_engaged": 200, "profile_views": 30, "profile_links_taps": 0}}
    cur = {"ig": {"followers": 40, "posts": {}}}
    out = dash.render(tmp_path, "2026-11-27", ig, {}, history, cur, {"dm": {"week": {"dms": 4, "tgClicks": 0}}},
                      [("Reel ready", "success", "")], [], {}, {}, {}, now=NOW, series_dir=tmp_path)
    assert out["level"] == "warn" and out["headline"]
    assert set(out["images"]) >= {"views24", "saves24"}
    for cid in out["images"]:
        assert f"cid:{cid}" in out["html"] and out["images"][cid][:8] == b"\x89PNG\r\n\x1a\n"
    assert "Where people drop off" in out["html"] and "This week" in out["html"] and "Do this week" in out["html"]
    assert "8,804" not in out["html"] and "9,000</b> reached" in out["html"] and "tapped link ❌" in out["html"]


def test_report_build_puts_dashboard_on_top(run_py, reel_folder):
    report = load("tools/report/report.py")
    top = {"level": "bad", "headline": "Reel voice wrong", "html": "<p>TOP</p>", "text": ["TOP"], "images": {}}
    subject, text, body, attachment = report.build(reel_folder, "2026-11-15", "", {}, {}, 60, [], top=top)
    assert subject == "❌ Job Shorts 2026-11-15: Reel voice wrong"
    assert "TOP" in body and "Reel ready" not in body          # the mail is the short part only
    assert "all numbers" in attachment and "Reel ready" in attachment
    assert text.splitlines()[2] == "TOP"


def test_chips(dash):
    steps = [("Reel ready", "success", ""), ("Instagram reel", "success", ""), ("YouTube Short ('subscribe' version)", "failure", ""),
             ("Instagram trial reel", "skipped", ""), ("LinkedIn post", "success", "")]
    assert dash.chips(steps) == [("Reel", "✅"), ("Insta", "✅"), ("YouTube", "❌"), ("LinkedIn", "✅")]


def test_linkedin_per_post_shapes():
    report = load("tools/report/report.py")
    raw = {"elements": [{"share": "urn:li:share:2", "totalShareStatistics": {"impressionCount": 120, "clickCount": 3, "engagement": 0.05}}]}
    flat = {"posts": [{"urn": "urn:li:share:1", "impressions": 80, "likes": 2}]}
    assert report.linkedin_posts(raw) == {"urn:li:share:2": {"impressions": 120, "clicks": 3, "engagement": 0.05}}
    assert report.linkedin_posts(flat) == {"urn:li:share:1": {"impressions": 80, "likes": 2}}
    assert report.linkedin_posts({"posts": '[{"urn": "u", "clicks": 1}]'}) == {"u": {"clicks": 1}}
    ids = report.linkedin_post_ids({"seed_posts": ["urn:li:share:5"], "max_posts": 3},
                                   [{"li": {"post_ids": ["urn:li:share:7", "urn:li:share:6"]}}], {"linkedin_post_id": "urn:li:share:9"})
    assert ids == ["urn:li:share:9", "urn:li:share:7", "urn:li:share:6"]
