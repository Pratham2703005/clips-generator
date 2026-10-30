# normalize-video

Turns raw recordings into clips that all have the same size, frame rate and format, ready to use as reel backgrounds.

## Needs
- Python 3.11+ (standard library only)
- FFmpeg on PATH (`ffmpeg` and `ffprobe`). Windows: `winget install --id Gyan.FFmpeg -e`, then open a new terminal.

## Use
```
python tools/normalize-video/normalize_video.py
```
Processes every video in the input folder whose output is missing, older than the recording, or was made with different settings. Run it again after every new recording. Finished files are skipped.

```
python tools/normalize-video/normalize_video.py clip2.mp4              # only this file (name inside the input folder)
python tools/normalize-video/normalize_video.py --force                # redo everything
python tools/normalize-video/normalize_video.py --config other.toml    # another settings file
python tools/normalize-video/normalize_video.py --input D:/rec --output D:/out --width 1080 --height 1080 --keep-audio
```

## Settings
Everything lives in [config.toml](config.toml):

| Setting | Default | What it does |
|---|---|---|
| `paths.input_dir` / `paths.output_dir` | `public/bg-minecraft-video/raw` / `input` | Where recordings are read from and written to |
| `paths.extensions` | mp4, mkv, mov, webm | Which files count as videos |
| `video.width` / `video.height` | 1080 x 1920 | Output size (even numbers) |
| `video.fit` | `crop` | `crop` fills the frame and cuts the overflow from the centre; `pad` fits the whole picture and adds bars (`pad_color`) |
| `video.fps` | 30 | Output frame rate; 0 keeps the source's |
| `video.crf` / `video.preset` | 28 / medium | x264 quality and speed. Lower crf = better and bigger |
| `audio.keep` / `audio.bitrate` | false / 128k | Keep the sound (AAC) or drop it |
| `trim.start` / `trim.end` | 3 / 3 | Seconds cut from the start and end |
| `trim.margin` | 6 | Pixels cut off every edge first (window borders, taskbar lines) |

- Paths in the config are relative to the config file. Paths given as `--input` / `--output` are relative to the current folder.
- Video, audio and trim settings can be overridden for one run with flags: `--width --height --fit --fps --crf --preset --keep-audio/--no-audio --trim-start --trim-end --margin`.

## Output
- `<output_dir>/<name>.mp4`: H.264, faststart (can be read from any offset).
- `<output_dir>/manifest.json`: one entry per file with its duration, size, fps and the settings used. Other tools read the durations from here instead of opening the videos.
- Each file is written as `<name>.part.mp4` first and only renamed after a check (size, audio, duration), so a stopped run never leaves a broken file that looks finished.

## Example
clip1 (1918x1090, 30 min, 1.9 GB, game audio) becomes 1080x1920, 30 fps, no audio, about 700 MB.
