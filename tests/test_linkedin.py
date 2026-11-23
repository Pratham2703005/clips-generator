"""LinkedIn (2026-11-23): the post's text, the Make webhook round trip, the report's LinkedIn stats and the report-only
mode. Nothing here opens a network connection."""

from __future__ import annotations

import io
import json
import tomllib

from conftest import ROOT, load

li = load("tools/linkedin-publisher/publish.py")
rp = load("tools/report/report.py")
CFG = tomllib.loads((ROOT / "tools/linkedin-publisher/config.toml").read_text(encoding="utf-8"))["linkedin"]
JOBS = [{"title": "Data Analyst", "company": "Acme", "where": "Pune", "apply_url": "https://example.com/1"},
        {"title": "QA Intern", "company": "Beta", "where": "Noida", "apply_url": "https://example.com/2"},
        {"title": "Third", "company": "Gamma", "where": "Delhi", "apply_url": "https://example.com/3"}]


def series_description(day: str) -> str:
    return (ROOT / "series" / f"{day}.txt").read_text(encoding="utf-8").split("--- description", 1)[1].strip()


def test_text_has_the_lesson_the_short_two_jobs_and_telegram():
    text = li.build_text(series_description("2026-11-23"), "https://youtube.com/shorts/abc", JOBS[:2], CFG)
    assert text.startswith("Low CGPA pe sorry mat bolo")
    assert "1. Sach" in text and "Sample:" in text
    assert "▶️ 30 second ki video: https://youtube.com/shorts/abc" in text
    assert "• Data Analyst at Acme, Pune: https://example.com/1" in text and "Third" not in text
    assert "Aaj ki aur jobs: https://t.me/fresherjobs_isha" in text
    # Instagram-only lines stay out: the DM ask, the "Kal" line; three hashtags at the end
    assert "comment karo" not in text and "Kal:" not in text
    assert text.splitlines()[-1].count("#") == 3


def test_day_jobs_prefers_the_telegram_list(tmp_path):
    (tmp_path / "daily-jobs.json").write_text(json.dumps(JOBS), encoding="utf-8")
    (tmp_path / "brief.json").write_text(json.dumps({"jobs": [{"title": "X", "company": "Y", "apply_url": "u"}]}), encoding="utf-8")
    assert [j["company"] for j in li.day_jobs(tmp_path, 2)] == ["Acme", "Beta"]
    (tmp_path / "daily-jobs.json").unlink()
    assert [j["company"] for j in li.day_jobs(tmp_path, 2)] == ["Y"]


def run_main(monkeypatch, folder, env: dict, answer: dict | None = None):
    sent = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        sent.append(json.loads(req.data))
        return Resp(json.dumps(answer or {}).encode())

    for k in ("MAKE_LINKEDIN_POST_WEBHOOK",):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(li.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(li, "cover_image", lambda *a, **k: None)
    monkeypatch.setattr(li, "load_env_files", lambda *a, **k: None)  # never the PC's real .env
    monkeypatch.setattr("sys.argv", ["publish.py", "--folder", str(folder)])
    li.main()
    return sent


def test_posts_through_the_webhook_and_records_it(tmp_path, monkeypatch):
    (tmp_path / "description.txt").write_text(series_description("2026-11-24"), encoding="utf-8")
    (tmp_path / "posted.json").write_text(json.dumps({"youtube_url": "https://youtube.com/shorts/x"}), encoding="utf-8")
    sent = run_main(monkeypatch, tmp_path, {"MAKE_LINKEDIN_POST_WEBHOOK": "https://hook.example/abc"},
                    {"post_id": "urn:li:share:123"})
    assert len(sent) == 1 and "Do rassi" in sent[0]["text"] and sent[0]["image_base64"] == ""
    assert "\(" in sent[0]["text"] and "{hashtag|\#|puzzle}" in sent[0]["text"]
    rec = json.loads((tmp_path / "posted.json").read_text(encoding="utf-8"))
    assert rec["linkedin_post_id"] == "urn:li:share:123"
    assert rec["linkedin_url"] == "https://www.linkedin.com/feed/update/urn:li:share:123/"
    assert run_main(monkeypatch, tmp_path, {"MAKE_LINKEDIN_POST_WEBHOOK": "https://hook.example/abc"}) == []  # once


def test_no_webhook_skips_quietly(tmp_path, monkeypatch, capsys):
    (tmp_path / "description.txt").write_text(series_description("2026-11-24"), encoding="utf-8")
    assert run_main(monkeypatch, tmp_path, {}) == []
    assert "not set" in capsys.readouterr().out
    assert not (tmp_path / "posted.json").exists()


def test_report_reads_linkedin_stats_and_tracks_growth(monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("MAKE_LINKEDIN_STATS_WEBHOOK", "https://hook.example/stats")
    monkeypatch.setattr(rp.urllib.request, "urlopen", lambda req, timeout=0: Resp(json.dumps(
        {"followers": "12", "impressions": 340, "clicks": 9, "likes": 4, "engagement": 0.05}).encode()))
    cfg = tomllib.loads((ROOT / "tools/report/config.toml").read_text(encoding="utf-8"))["linkedin"]
    li_stats = rp.linkedin_stats(cfg)
    assert li_stats["followers"] == 12 and li_stats["totals"]["impressions"] == 340
    snap = rp.snapshot({}, {}, others={"li": li_stats})
    prev = {"li": {"followers": 10, "totals": {"impressions": 300, "clicks": 5}}}
    rows = {name: (now, ch) for name, now, ch in rp.growth_rows(snap, [prev, None])}
    assert rows["LinkedIn followers"][1][0] == "+2"
    assert rows["LinkedIn impressions (all posts)"][1][0] == "+40"
    monkeypatch.delenv("MAKE_LINKEDIN_STATS_WEBHOOK")
    assert rp.linkedin_stats(cfg) == {}


def test_little_text_escapes_and_keeps_hashtags():
    assert li.little_text("Fix (3 step) a_b #fresherjobs") == "Fix \(3 step\) a\_b {hashtag|\#|fresherjobs}"
