# caption-maker

Times every word of the script against the voice, then makes captions in 2-3 word chunks. Each character gets its own colour and the word being spoken is highlighted. It can also burn the captions into a video.

## Needs
- Python 3.11+ with `faster-whisper` (`pip install faster-whisper`). The model (~150 MB) downloads on the first run, then loads from the cache.
- FFmpeg on PATH (or set `paths.ffmpeg`)

## Use
```
python tools/caption-maker/make_captions.py                          # latest script, timed against its voice.wav
python tools/caption-maker/make_captions.py --date 2026-10-29
python tools/caption-maker/make_captions.py --audio other.wav        # time against another audio file
python tools/caption-maker/make_captions.py --video reel.mp4         # time against the video's audio and burn the captions in
```
It takes a few seconds: about 1 s to load the model, 1-2 s per Whisper pass, and about 8 s to burn 18 s of 1080x1920 video.

## Output (next to the script)
- `timeline.json`: each line's speaker, start and end, and each word's start and end, marked `matched` when Whisper heard it. The composer uses it to slide the characters in and out.
- `captions.ass`: the styled captions (ASS subtitles, 1080x1920).
- With `--video`: `<video name>.captioned.mp4`, with the audio as stereo AAC 48 kHz (Instagram's format; plays in VLC and Windows Media Player, but not in VS Code's preview, which has no AAC).

## How the timing works
1. FFmpeg decodes the audio to 16 kHz mono, and Whisper (`faster-whisper`) lists the words it hears with their times. A second pass with the script as a hint runs too, and the pass that matches more of the script is kept. The hint is never used alone, because with it Whisper skipped the opening lines.
2. The script's words are lined up with the heard words, ignoring case and punctuation. A matched word takes Whisper's time. Words Whisper missed or heard differently ("400 thousand" heard as "400,000") share the gap between their matched neighbours.
3. Caption text always comes from the script, so names are spelled right even when Whisper mishears them.

If less than `whisper.min_match` of the words match, it prints a warning, because the audio may not follow the script.

## Settings ([config.toml](config.toml))
| Setting | Default | What it does |
|---|---|---|
| `paths.default_audio` | `voice.wav` | Audio next to the script used when no `--audio`/`--video` is given |
| `paths.ffmpeg`, `paths.fonts_dir` | empty, `public/fonts` | ffmpeg location; font files for burning in |
| `whisper.model`, `language` | `small`, `en` | Multilingual model for Hinglish; `en` makes it write Hindi words in Roman script, like the scripts |
| `whisper.try_script_prompt` | true | Also run the script-hint pass |
| `captions.max_words` | 3 | Words on screen at once (a chunk also ends at punctuation and at a speaker change) |
| `captions.look` | `glow` | `glow` = white text with a soft glow and light shadow in the speaker's colour; `shadow` = hard outline and drop shadow; `box` = solid box; `outline` = coloured outline only |
| `captions.border`, `shadow`, `shadow_opacity`, `blur` | 3, 4, 70, 2 | Outline width (or box padding), shadow offset and strength, edge softness |
| `captions.glow_size`, `glow_blur`, `glow_opacity` | 14, 12, 55 | Spread, softness and strength of the glow |
| `captions.font`, `size`, `uppercase` | Archivo Black (public/fonts), 100, true | Text style |
| `captions.text_color`, `highlight_color` | white, `#FFE600` | Normal text and the word being spoken |
| `captions.margin_bottom` | 560 | Height above the bottom edge (just above the composer's characters, below its screenshot panel) |
| `captions.pop_from`, `pop_ms` | 85, 90 | Each chunk pops in from 85% size; 100 turns it off |
| `captions.hold_gap` | 0.4 | Gaps shorter than this keep the previous chunk on screen |
| `speakers.<id>` | | Caption colour per character: Arjun blue, Bunty orange, Riya pink, Verma green, Malhotra purple |
