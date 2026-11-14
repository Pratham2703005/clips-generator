"""The publishers' text and posting logic, with fake APIs: nothing leaves this machine."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from conftest import load

ig = load("tools/instagram-publisher/publish.py")
fb = load("tools/facebook-publisher/publish.py")
th = load("tools/threads-publisher/publish.py")
tg = load("tools/telegram-publisher/publish.py")
yt = load("tools/youtube-publisher/publish.py")

POLL = {"every_seconds": 0, "max_minutes": 1}
REFUSED = 'POST upload: HTTP 400: {"retriable": false, "type": "ProcessingFailedError", "message": "Request processing failed"}'


# ---------- Instagram ----------

class FakeIg:
    """Graph API + upload host. upload_fails: how many uploads ProcessingFailedError refuses (99 = all)."""
    version = "v26.0"

    def __init__(self, upload_fails=0, create_error=None):
        self.upload_fails, self.create_error, self.calls = upload_fails, create_error, []

    def call(self, method, path, fields=None, url=None, body=None, headers=None):
        self.calls.append((method, path or "upload", dict(fields or {})))
        if url:
            if self.upload_fails > 0:
                self.upload_fails -= 1
                raise RuntimeError(REFUSED)
            return {"success": True}
        if path.endswith("/media"):
            if self.create_error:
                raise RuntimeError(self.create_error)
            return {"id": "C1", "uri": "https://rupload.example/x"}
        if method == "GET":
            return {"status_code": "FINISHED", "permalink": "https://instagram.com/reel/x"}
        if path.endswith("media_publish"):
            return {"id": "M1"}
        raise AssertionError(f"unexpected call {method} {path}")


class FakeHosted:
    def __init__(self):
        self.gets = 0

    def get(self):
        self.gets += 1
        return "https://github.com/o/r/releases/download/reel-uploads/a.mp4"


def video(tmp_path):
    v = tmp_path / "reel.mp4"
    v.write_bytes(b"x" * 3000)
    return v


def test_ig_upload_in_one_piece(tmp_path):
    api = FakeIg()
    assert ig.post(api, "U", video(tmp_path), "REELS", {"caption": "c"}, POLL, 8, FakeHosted()) == "M1"
    assert sum(1 for c in api.calls if c[1] == "upload") == 1


def test_ig_pieces_after_a_refused_upload(tmp_path):
    api, hosted = FakeIg(upload_fails=1), FakeHosted()
    assert ig.post(api, "U", video(tmp_path), "REELS", {}, POLL, 8, hosted) == "M1"
    assert hosted.gets == 0


def test_ig_video_url_when_uploads_keep_failing(tmp_path):
    """2026-11-13: every upload refused; the reel must still go up, from the temporary link."""
    api, hosted = FakeIg(upload_fails=99), FakeHosted()
    assert ig.post(api, "U", video(tmp_path), "STORIES", {}, POLL, 8, hosted) == "M1"
    assert hosted.gets == 1
    made = [c[2] for c in api.calls if c[1] == "U/media"]
    assert "video_url" in made[-1] and "upload_type" not in made[-1]


def test_ig_without_fallback_the_error_shows(tmp_path):
    with pytest.raises(RuntimeError, match="ProcessingFailedError"):
        ig.post(FakeIg(upload_fails=99), "U", video(tmp_path), "REELS", {}, POLL, 8, None)


def test_ig_other_errors_are_not_retried(tmp_path):
    api = FakeIg(create_error="POST media: HTTP 400: Invalid parameter (code 100, subcode 2207026)")
    with pytest.raises(RuntimeError, match="2207026"):
        ig.post(api, "U", video(tmp_path), "REELS", {}, POLL, 8, FakeHosted())
    assert len(api.calls) == 1


def test_ig_trial_not_eligible_is_recorded(reel_folder):
    vf = reel_folder / "variants" / "trial"
    vf.mkdir(parents=True)
    (vf / "reel.mp4").write_bytes(b"x" * 100)
    cfg = {"poll": POLL}
    igc = {"caption_max": 2200, "hashtags_max": 5, "cover_seconds": 2.2}
    ig.post_trial(FakeIg(create_error="HTTP 400: not eligible (code 9, subcode 2207081)"), "U", reel_folder, igc, cfg, False)
    rec = json.loads((reel_folder / "posted.json").read_text(encoding="utf-8"))
    assert rec["trial_status"] == "not eligible"


def test_ig_caption_keeps_five_hashtags(reel_folder):
    (reel_folder / "description.txt").write_text("Hi\n#a #b #c #d #e #f #g", encoding="utf-8")
    cap = ig.caption_for(reel_folder, {"caption_max": 2200, "hashtags_max": 5})
    assert cap.split() == ["Hi", "#a", "#b", "#c", "#d", "#e"]


# ---------- Threads ----------

def test_threads_question_only(reel_folder):
    (reel_folder / "threads.txt").write_text("Weakness kya batate ho?\n", encoding="utf-8")
    assert th.threads_text(reel_folder, 500, "https://site") == "Weakness kya batate ho?\n\n#interviewtips"
    assert th.threads_text(reel_folder, 500, "https://site", topic_tag=False) == "Weakness kya batate ho?"


def test_threads_falls_back_to_the_description(reel_folder):
    t = th.threads_text(reel_folder, 500, "https://site")
    assert t.endswith("#interviewtips") and t.count("#") == 1
    assert "link in bio" not in t and "https://site" in t
    assert len(th.threads_text(reel_folder, 120, "")) <= 120


# ---------- Facebook ----------

def test_facebook_without_links(reel_folder):
    note = "Apply links: Telegram pe fresherjobs_isha search karo."
    cap = fb.caption_for(reel_folder, 2200, "https://site", links=False, links_note=note)
    assert "http" not in cap and "link in bio" not in cap.lower()
    assert note in cap and cap.splitlines()[-1].startswith("#")
    assert "- Data Analyst at Infosys" in cap  # the job itself stays, only its link goes


def test_facebook_with_links(reel_folder):
    cap = fb.caption_for(reel_folder, 2200, "https://site")
    assert "every day: https://site" in cap


# ---------- Telegram ----------

def test_telegram_channel_text(reel_folder):
    (reel_folder / "brief.json").write_text(json.dumps({"platform": {"id": "n8n", "name": "n8n", "url": "https://n8n.io/workflows/1"}}),
                                            encoding="utf-8")
    t = tg.channel_text(reel_folder)
    assert t.startswith("Interview prep | Test")
    assert "Job in this reel:\n- Data Analyst at Infosys" in t and "Apply: https://career.infosys.com" in t
    assert t.endswith("N8n: https://n8n.io/workflows/1")
    cap, rest = tg.caption_parts(reel_folder, 40, 4096)
    assert len(cap) <= 40 and rest


def test_telegram_value_episode_has_no_jobs(reel_folder):
    (reel_folder / "description.txt").write_text("Search line\nInterview prep\nTips.\n\n#a", encoding="utf-8")
    assert tg.channel_text(reel_folder) == "Interview prep | Test"


# ---------- YouTube ----------

def test_youtube_title():
    t = yt.make_title("Interview prep | Strengths and weaknesses",
                      "Strengths and weaknesses answer for freshers | What is your weakness interview", 92)
    assert t.startswith("Strengths and weaknesses answer for freshers") and len(t) <= 92


def test_youtube_metadata(reel_folder):
    v = {"title_max": 100, "shorts_tag": True, "description_max": 5000, "category_id": "27", "language": "hi",
         "privacy": "private", "made_for_kids": False, "synthetic_media": True, "site_url": "https://site"}
    md = yt.metadata(reel_folder, v)
    assert md["snippet"]["title"].endswith("#Shorts") and len(md["snippet"]["title"]) <= 100
    assert "every day: https://site" in md["snippet"]["description"]
    assert md["snippet"]["tags"][:2] == ["interviewtips", "hrinterview"]


def test_youtube_publish_time():
    early = datetime(2026, 11, 13, 0, 5, tzinfo=yt.IST).astimezone(timezone.utc)  # the nightly run, 00:05 IST
    when = yt.publish_time("19:00", early)
    assert when and when.hour == 19
    late = datetime(2026, 11, 13, 18, 50, tzinfo=yt.IST)
    assert yt.publish_time("19:00", late) is None  # under 15 min away: public right away


def test_youtube_config_publishes_at_seven_pm():
    """User, 2026-11-14: every Short goes public at 19:00 IST."""
    import tomllib
    from conftest import ROOT
    cfg = tomllib.loads((ROOT / "tools/youtube-publisher/config.toml").read_text(encoding="utf-8"))
    assert cfg["video"]["publish_at"] == "19:00"


def test_youtube_cover_without_ffmpeg(reel_folder, monkeypatch):
    """2026-11-10: the Post reel runner had no ffmpeg and the step failed after the upload."""
    monkeypatch.setattr(yt.shutil, "which", lambda name: None)
    assert yt.cover_frame(reel_folder, reel_folder / "reel.mp4", 2.2) is None
