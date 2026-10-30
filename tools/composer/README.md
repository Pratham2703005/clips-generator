# composer

Puts the day's reel together: a stretch of background gameplay, the speaking character sliding in and out, the voice, and the captions.

## Needs
- Python 3.11+ with Pillow (`pip install pillow`)
- FFmpeg on PATH (or set `paths.ffmpeg_dir`)
- In the day's folder: `script.json` (script-writer), `voice.wav` (voice-generator), `timeline.json` and `captions.ass` (caption-maker)
- At least one clip in `public/bg-minecraft-video/input/` (normalize-video)

## Use
```
python tools/composer/compose.py                      # latest day with captions
python tools/composer/compose.py --date 2026-10-29
python tools/composer/compose.py --reroll 1           # same reel, another stretch of background
python tools/composer/compose.py --clip clip1.mp4     # only this background clip
```
An 18 s reel renders in about 20 s.

## Output (in the day's folder)
- `reel.mp4`: 1080x1920, 30 fps, H.264, stereo AAC 48 kHz. Ready for Instagram.
- `compose.json`: which clip and offset were used, and when each character was on screen.

## What happens
- **Background:** one clip from `backgrounds_dir`, starting at a random point. The pick is seeded by the date, so running again gives the same reel. It is slightly dimmed so the captions and characters stand out.
- **Characters:**
  - Consecutive lines by the same speaker form one turn, and only the speaker is on screen.
  - A turn slides in from the character's own side just before the first word. It slides out as the next speaker slides in from the other side.
  - The image follows the line's expression (`talk`, or `shock` for shocked lines).
  - Images are trimmed to the character, with one shared box per pack so switching expression doesn't shift it. They are scaled to `characters.height` and mirrored when the character would face away from the middle. `no_flip` images are never mirrored.
- **On-screen layer** (`overlay.ass`, drawn over the captions):
  - The script's `hook_card` lands word by word in the first ~1.9 s, in the top safe zone (below y=230).
  - A leading number counts up from 0 ("200" races up in 1 s).
  - From a "0"/"ZERO"/"NO" word on, the words slam in red with the stamp sound ("0 CALLS.").
  - A job card (company, title, city, "posted today") drops in while a job's line is spoken.
  - A red "REJECTED" stamp slams in on the spoken word "Rejected". Stamps are set in `[overlay.stamps]`.
- **Screenshot panel:**
  - If visual-capture wrote `visuals.json`, each screenshot appears in a 1040x600 bordered panel (y 530) while its line is spoken.
  - The page scrolls down like a screen recording (90 px/s; the hook tracker uses its own speed) and fades in and out.
  - A panel on line 1 is up from frame 0, with no fade-in.
- **Sound:**
  - Optional SFX: a whoosh on every character change, a pop on job cards, a thud on stamps.
  - Optional music bed at -24 dB, looped from any file in `public/music/`.
  - Put royalty-free files in `public/sfx/` (`whoosh.mp3`, `pop.mp3`, `stamp.mp3`, `shock.mp3`). A missing file is skipped with a note.
- **Fonts:** the on-screen layer uses Archivo Black from `public/fonts` (`overlay.fonts_dir`), so it looks the same on any machine.
- **Layout:**
  - The first speaker is on screen at frame 0.
  - Each figure stays in its outer ~36% (`inner_edge`). Placement follows the idle image's figure, so a waving arm in another expression sticks out past the frame edge.
  - A figure too wide for that is drawn smaller, so at most `bleed` (30%) of it is cut off.
  - Characters sit on the bottom edge: the packs are half-body images.
  - The background stretch is the most moving of 4 random tries, with points taken off for glare.
- **Voice and captions** go on top. The captions come from caption-maker's `captions.ass`, so their look is set in caption-maker's config.

## Settings ([config.toml](config.toml))
| Setting | Default | What it does |
|---|---|---|
| `paths.backgrounds_dir`, `packs_dir` | public/... | Where the clips and character packs are |
| `video.tail` | 0.8 | Seconds of background after the last word |
| `background.margin` | 5 | Keep away from the clip's start and end |
| `background.brightness`, `saturation` | -0.05, 0.9 | Dim and desaturate the gameplay a little |
| `characters.height` | 700 | Character height in pixels (caption-maker's `margin_bottom` 560 keeps the captions on the characters' chest line) |
| `characters.inner_edge`, `bleed` | 0.36, 0.3 | How far in from its side a figure may reach; how much of a too-wide figure may be cut off |
| `overlay.*`, `panel.*`, `audio.*` | | On-screen layer, screenshot panel, SFX and music (see comments in the file) |

These defaults can be overridden by the root `config.toml` (`[compose.*]`) and the run mode (`modes/*.toml`), see the root README.
| `characters.margin_side`, `margin_bottom` | 10, 0 | Gap from the side and bottom edges |
| `characters.slide`, `lead` | 0.25, 0.12 | Slide time; how early the slide-in starts before the first word |
| `characters.normal`, `shocked`, `idle` | talk, shock, idle | Image per expression |
| `packs.<pack>.images`, `no_flip` | | Per pack: map an expression to another image (fresher: shocked -> talk); images never to mirror |

Each line shows one image: `talk`, or `shock` for shocked lines (a pack can remap it).
