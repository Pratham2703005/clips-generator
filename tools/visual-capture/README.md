# visual-capture

Screenshots the real pages a reel talks about, so the reel shows them instead of only saying them: the job's own apply page, and our product pages (landing page, Apify, RapidAPI, api.market, Zyla, the n8n and Make templates). Runs between caption-maker and composer.

## Needs
- Python 3.11+
- Chrome, Edge or Chromium (found from `[chrome].paths`)
- `websocket-client` (`pip install websocket-client`). It drives Chrome over the DevTools protocol, so cookie popups can be removed and tall shots taken. Without it, Chrome's plain `--screenshot` is used, which can't do either.

## Use
```
python tools/visual-capture/capture.py --date 2026-10-30
python tools/visual-capture/capture.py --fresh        # ignore the product-page cache
```
About 5-15 s per page.

## What it shows where
- A line with a `job_id` gets that job's apply page: proof the job is real and fresh.
- A line naming a product gets that product's page. The `[[rules.mentions]]` regexes map words to `[pages]`, for example "n8n" to the n8n template.
- The last line (the call to action) gets the day's platform page from the brief.
- At most `max_visuals` per reel.

- **The hook's own visual:** when line 1 has a number ("You sent 200 applications..."), it draws an illustrative "My job applications" tracker with that many "No response" rows.
  - It is our own HTML, labelled "illustration", with generic roles and no real companies. It is never a copy of a real app's UI.
  - The composer shows it from frame 0, scrolling fast (`hook_scroll`).

## How a shot is taken
1. Each shot uses a throwaway Chrome profile: no cookies, no logins, nothing kept.
2. The viewport is a 1280x800 desktop monitor at 2x, so pages show their desktop layout (the user's call). The image is 2560 px wide.
3. After the load event it waits until the page text stops growing (`settle_ms`, at most `wait_ms`). Workday pages are skeletons at load.
4. It removes cookie and consent popups and other fixed overlays.
5. It saves a shot taller than the screen (2000 css px), so the composer has page to scroll.
- Product pages are cached in `public/visual-cache` for `cache_days`. Job pages are taken fresh every time.

## Checks
A job page whose text matches `job_red_flags` (volunteer, unpaid, closed, no longer accepting...) gets a WARNING and a `warnings` entry in visuals.json. The API data can't show these. On 2026-10-30 a "Technical Writer" job was a volunteer role, and only its page said so.

## Output (in the day's folder)
- `visuals/*.png` (and `*.txt` with each page's text, used by the checks)
- `visuals.json`: `{items: [{line, kind, key, url, file, text, warnings?}]}`. The composer shows each one in a panel while its line is spoken.
