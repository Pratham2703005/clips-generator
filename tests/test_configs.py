"""Every config file parses, and the master config and the run modes only set keys the tools really have (a typo there
is otherwise silently ignored by the tool and only printed to stderr)."""

from __future__ import annotations

import tomllib

import pytest

from conftest import ROOT, load

TOML_FILES = [ROOT / "config.toml", *sorted((ROOT / "modes").glob("*.toml")), *sorted((ROOT / "tools").glob("*/*.toml"))]
LAYERED_TOOLS = [
    "tools/caption-maker/make_captions.py",
    "tools/composer/compose.py",
    "tools/content-data-preparation/prepare_content.py",
    "tools/script-writer/write_script.py",
    "tools/visual-capture/capture.py",
    "tools/voice-generator/generate_voice.py",
]
MODES = [None, *sorted((ROOT / "modes").glob("*.toml"))]


@pytest.mark.parametrize("path", TOML_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_toml_parses(path):
    tomllib.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.name if m else "no-mode")
@pytest.mark.parametrize("tool", LAYERED_TOOLS, ids=lambda t: t.split("/")[1])
def test_overlays_only_use_known_keys(tool, mode, monkeypatch, capsys):
    if mode:
        monkeypatch.setenv("VG_MODE_FILE", str(mode))
    mod = load(tool)
    cfg = mod.load_config(mod.DEFAULT_CONFIG)
    assert isinstance(cfg, dict) and cfg
    err = capsys.readouterr().err
    assert "is not in the tool's own config.toml" not in err, err


def test_publisher_configs_have_what_the_code_reads():
    ig = tomllib.loads((ROOT / "tools/instagram-publisher/config.toml").read_text(encoding="utf-8"))["instagram"]
    for k in ("api_version", "token_env", "user_env", "caption_max", "hashtags_max", "release_tag"):
        assert k in ig, f"[instagram] {k}"
    th = tomllib.loads((ROOT / "tools/threads-publisher/config.toml").read_text(encoding="utf-8"))["threads"]
    for k in ("token_env", "user_env", "text_max", "release_tag", "token_expires"):
        assert k in th, f"[threads] {k}"
