# Job Shorts video generator

Makes a ~45 s vertical reel (Hinglish, two cartoon characters over gameplay) promoting the ATS Jobs API, one a day.

```
brief -> script -> voice -> captions -> visuals -> compose -> reel.mp4
```

| Step | Tool | What it does |
|---|---|---|
| brief | [content-data-preparation](tools/content-data-preparation/README.md) | Picks today's jobs, track, cast and idea (premise, hook formula, dynamic) |
| script | [script-writer](tools/script-writer/README.md) | An LLM writes the dialogue; checks and a judge model send weak drafts back |
| voice | [voice-generator](tools/voice-generator/README.md) | Gemini TTS, one voice per character, sped up to a target pace |
| captions | [caption-maker](tools/caption-maker/README.md) | Whisper word timings, glow captions (`captions.ass`) |
| visuals | [visual-capture](tools/visual-capture/README.md) | Headless Chrome screenshots of the job and product pages the script mentions |
| compose | [composer](tools/composer/README.md) | FFmpeg puts it together: background, characters, hook card, panels, SFX |
| (once) | [normalize-video](tools/normalize-video/README.md) | Turns raw gameplay recordings into 1080x1920 background clips |

## Setup
- Python 3.11+, FFmpeg (with libass), and Chrome/Chromium/Edge
- `pip install -r requirements.txt`
- A `.env` in the repo root with `GEMINI_API_KEY`, `GROQ_API_KEY` and `JOB_FEED_API_KEY`
- Background clips in `public/bg-minecraft-video/input/` (make them with normalize-video, or download the `backgrounds` release)

## Make a reel
```
python run.py ats                         # today's ATS Jobs API reel -> content/<date>/reel.mp4
python run.py custom                      # your own script (modes/custom.toml) -> content/custom/<name>/reel.mp4
python run.py custom my-script.txt --left malhotra --right riya --visuals off
```

Options, each overriding the mode file: `--left` / `--right` (characters), `--hook "TEXT"`, `--visuals on|off`, `--clip clip2.mp4`, `--name`, `--date YYYY-MM-DD`.

## Modes
- **ats** ([modes/ats.toml](modes/ats.toml)): fully automatic. Fresh jobs, an AI script, fact checks, visuals of the real pages.
- **custom** ([modes/custom.toml](modes/custom.toml)): starts as a copy of ats, then you choose. Give it your own script and it is made exactly as written (no rewriting, no checks). With no script it behaves like ats, but you can still pick the characters or turn visuals off.

A custom script is plain text ([example](modes/example-script.txt)):
```
hook: ZERO CALLS. ZERO REPLIES.
Riya: Ma'am, mujhe call kyun nahi aata?
Malhotra (shocked): Kyunki tum har jagah apply karti ho.
Riya: Toh kahan karun? [show: https://ats-jobs-api.pk2732004.workers.dev]
```
- `Name: text` is a spoken line. Names can be the id or the name: `arjun`, `bunty`, `riya`, `verma`, `malhotra`.
- `(shocked)` uses the shocked face.
- `[show: URL]` puts a screenshot of that page on screen during the line.
- `hook:` is the on-screen hook card. Lines starting with `#` are ignored.
- The first speaker is on the left, unless `--left` / `--right` say otherwise.

Another mode is just another `.toml` in `modes/`: `python run.py modes/my-mode.toml`.

## Settings
Three layers, later ones win:

```
tools/<tool>/config.toml   <   config.toml (master)   <   modes/<mode>.toml
```
- **Tool config**: every tool keeps a full set of values, so each one also runs on its own (`python tools/composer/compose.py --date ...`).
- **Master [config.toml](config.toml)**: the values worth tuning from one place. A table like `[compose.panel]` overrides `[panel]` in the composer's config. A key the tool doesn't have is reported.
- **Mode file**: `[run]` holds the run choices. Any tool table can be added there too, to change it for that mode only (e.g. `[compose.overlay] enabled = false`).

Paths in an override are relative to the file they are written in. Each tool reads the master from `$VG_MASTER` (default: the root `config.toml`) and the mode from `$VG_MODE_FILE`, which `run.py` sets.

## Output
- `content/<date>/` (ats) or `content/custom/<name>/` (custom): `brief.json`, `script.json` / `script.md`, `voice.wav`, `captions.ass`, `visuals.json`, `reel.mp4`
- `content/history.json`: which jobs, premises, hooks and dynamics were used, so reels don't repeat. This is the only content file kept in git.

## GitHub Actions (daily)
[.github/workflows/daily-reel.yml](.github/workflows/daily-reel.yml) runs `python run.py ats` at 08:00 IST and keeps the reel as a downloadable artifact for 30 days. It can also be started by hand (Actions → Daily reel → Run workflow) with mode `custom` and a script path in the repo.

One-time setup:
1. **Secrets** (Settings → Secrets and variables → Actions): `GEMINI_API_KEY`, `GROQ_API_KEY`, `JOB_FEED_API_KEY`.
2. **Background clips**: create a release tagged `backgrounds` and attach `clip1.mp4`, `clip2.mp4` (from `public/bg-minecraft-video/input/`). They are too big for git; the workflow downloads them.
   ```
   gh release create backgrounds public/bg-minecraft-video/input/clip1.mp4 public/bg-minecraft-video/input/clip2.mp4 --title "Background clips" --notes "Processed gameplay for the composer"
   ```
3. **Workflow permissions**: Settings → Actions → General → Workflow permissions → "Read and write", so the run can commit `content/history.json`.

The Whisper model (~460 MB) is cached between runs.

## Credits
- Font: [Archivo Black](public/fonts/OFL.txt) (SIL Open Font License)
