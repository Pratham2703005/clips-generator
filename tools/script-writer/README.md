# script-writer

Turns a day's `brief.json` (from content-data-preparation) into the reel's dialogue with Gemini, and checks it before saving.

## Needs
- Python 3.11+ (standard library only)
- A Google AI Studio key in `GEMINI_API_KEY`, in the environment or in the project's `.env` (git-ignored)

## Use
```
python tools/script-writer/write_script.py --print                 # latest brief in content/, print the result
python tools/script-writer/write_script.py --date 2026-10-29
python tools/script-writer/write_script.py --brief path/to/brief.json
python tools/script-writer/write_script.py --model gemini-3.8-flash # one model only
```

## Output (next to the brief)
- `script.json`: title, cast, lines (speaker id and name, text, expression `normal`/`shocked`, the job each line is about), Instagram caption (the model's hook plus a job list and "Find it" line built from the brief, then hashtags), word count, model used.
- `script.md`: the same, easy to read and judge.

## Checks (a failing script goes back to the model with the problems, up to `attempts` times per model)
- 12-20 lines, at most 18 words per line, 150-170 words in total (a ~44-50 second reel at 1.25x, ~3.4 words per second)
- a dialogue: nobody speaks more than 2 lines in a row, each character has at least 35% of the lines, only the brief's two speakers
- lines about specific jobs are at most 40% of the words (~30% data, ~70% story, banter and context)
- background claims come only from the brief's `context` and product facts
- every job in the brief is mentioned with its id, and no unknown ids
- **no number that isn't in the brief**, whether written as digits ("20,000", "20K", "1.9 million") or words ("twenty thousand"); honest roundings of the brief's numbers are fine
- the last line is the call to action ("link in bio")
- no URLs or hashtags in spoken lines; 5-10 hashtags

- `hook_card`: the on-screen hook, 1-6 words. The composer shows it at frame 0.
- The ig-reel skill's own scorers (`[quality]`, skipped if the skill isn't installed):
  - `hookscore.py` on line 1 must reach `min_hook_score` (70). The hook should speak to "you", with a number and some tension.
  - `beats.py` at our voice's rate (204 wpm) must not report "No loop" (the last line has to echo a word from the hook), empty middle beats, or a beat over 4 seconds.

## Settings ([config.toml](config.toml))
- `llm.models`: tried in order (on 404, rate limits, 503 "high demand", or repeated failed checks). On 2026-10-29 `gemini-3.5-flash` worked; `gemini-flash-latest` hung and `gemini-2.5-flash` is closed to new users.
- `script.*`: line/word/hashtag limits and the small numbers anyone may say.
- `style.rules`: the voice and the fact rules every script follows.
