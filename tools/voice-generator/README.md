# voice-generator

Turns a day's `script.json` (from script-writer) into the reel's voice track with Gemini text-to-speech. Each character always gets the same voice.

## Needs
- Python 3.11+ (standard library only)
- FFmpeg on PATH (or set `paths.ffmpeg`)
- A Google AI Studio key in `GEMINI_API_KEY`, in the environment or in the project's `.env` (git-ignored)

## Use
```
python tools/voice-generator/generate_voice.py                       # latest script in content/
python tools/voice-generator/generate_voice.py --date 2026-10-29
python tools/voice-generator/generate_voice.py --script path/to/script.json
python tools/voice-generator/generate_voice.py --new                 # ask Gemini again even if nothing changed
```
One request takes about 3 minutes on the free tier; a line is printed every 30 s while it waits.

## Output (next to the script)
- `voice.wav`: the final track, sped up with the pitch kept and loudness-normalised. This is what the reel uses.
- `voice.raw.wav`: Gemini's audio at normal speed. When the lines, voices and notes haven't changed it is reused, so changing `speed` or `loudness_lufs` and running again takes a second.
- `voice.json`: model, voice per character, speed, raw and final length.

## How it works
- The whole dialogue goes in one multi-speaker request: director notes (`voice.direction`, each character's `notes`, and the lines marked `shocked`), then the lines as `Label: text`. The characters then react to each other instead of reading separate clips.
- A reply with no audio, or audio too short or long for the number of words (cut off or garbled), is retried up to `tts.attempts` times per model.

## Settings ([config.toml](config.toml))
| Setting | Default | What it does |
|---|---|---|
| `paths.content_dir` | `../../content` | Where the dated folders with `script.json` are |
| `paths.ffmpeg` | empty | Full path to ffmpeg if it isn't on PATH |
| `tts.models` | `gemini-2.5-flash-preview-tts` | Tried in order |
| `tts.attempts` | 3 | Tries per model |
| `tts.min/max_words_per_second` | 1.5 / 5.0 | Sanity range for the raw audio |
| `voice.speed` | 1.25 | Speed-up after generation |
| `voice.loudness_lufs` | -14 | Loudness target; 0 skips it |
| `voice.direction` / `voice.shocked` | | How the whole dialogue and the shocked lines should sound |
| `characters.<id>` | | `label` (name in the transcript), `voice` (Gemini voice), `notes` (age, voice, attitude) |

Current cast (median pitch from the audition): Arjun = Algenib (113 Hz), Verma = Charon (103 Hz as an older man, picked by the user from the old-man audition), Bunty = Sadachbia (168 Hz), Malhotra = Sulafat (205 Hz), Riya = Leda (~240-258 Hz). A new character needs its own `[characters.<id>]` with a voice no one else in the script uses.

Before any API call, two characters closer than `audition.min_gap_semitones` (3) in pitch get a warning. Bunty on Puck (121 Hz) and Malhotra on Gacrux (122 Hz) sounded like one person.

## Voice audition (`audition.py`)
Measures how high or low each Gemini voice sits, so a cast can be picked by ear and by numbers.
```
python tools/voice-generator/audition.py                                         # voices in [audition].voices not measured yet
python tools/voice-generator/audition.py --measure some.wav --lines lines.json   # an existing dialogue, no API call
```
- Each request is a two-voice dialogue with plain direction, so one request measures two voices. Whisper times each line, and the median pitch and range per voice go to `voice-pitch.json` (in this folder, kept in git). The audio goes to `audition-audio/` (git-ignored) so you can listen.
- It is gentle on the free tier. Requests go one at a time, at least `min_interval` (25 s) apart. A 429 waits as long as Google asks. A daily-quota 429 stops the run and keeps progress, instead of retrying. A voice already measured is never requested again.
- 2026-10-30: 17 voices measured with 6 requests, from Algenib 113 Hz up to Leda 258 Hz. Gacrux, Puck, Charon and Enceladus all sit at 120-123 Hz.
