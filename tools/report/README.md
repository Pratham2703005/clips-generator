
## The top of the mail (dashboard.py, 2026-11-27)

The user found the old mail unreadable (a wall of numbers, "success" even on a broken reel). The mail now opens with:

1. **Verdict** (also the subject): the worst thing tonight, else how yesterday's reel did.
2. **Look at these first**: posting failures, the voice check, a TTS model fallback (quota), a missing episode file for
   tomorrow, yesterday's reel vs the usual (flop / hit), followers flat for 3 days, a profile link nobody taps, a
   YouTube views drop, tokens, and the `watch` notes of recent `changes.toml` entries. Passing checks share one line.
3. **Five numbers** against `[dashboard.targets]` (this week vs the week before, with an action when below):
   views at 24 h, saves+shares per 1,000 views at 24 h, followers per 1,000 views, watch time, people who came to us.
4. **Charts** (matplotlib PNGs inline; Gmail drops SVG/scripts): views at 24 h per reel vs the usual, saves+shares per
   1,000 views, followers. Reels are compared at the same age (~24 h, from the stats snapshots), never by totals.
5. **Where people drop off**: the profile path and the comment -> DM path, each with step-to-step rates.
6. **What changed**: `changes.toml` (what / why / watch) and the human commits of the last 3 days.

The old tables follow as "Details". `--preview FILE` writes the mail as one HTML file with the charts inline.
If the dashboard fails, the plain report still goes out.

Second pass (same day, user: "less text, simple words"): the mail is only the short part (Tonight chips, Fix, This week
tiles + "Do this week", charts, drop-off lines, Changed lately); every old table goes to `all-numbers-<day>.html`.

## LinkedIn per post (2026-11-27)

The report now sends `{"organization": "...", "shares": ["urn:li:share:...", ...]}` (newest first, up to 20) to the
stats webhook. To answer per post, in Make's **linkedin-stats** scenario:

1. After the Webhooks module, add **LinkedIn → Get the Statistics of Shares** (in the app's "Other" group). Pick the
   same connection and organization; in its shares field map the webhook's `shares` array.
2. In the **Webhook response** body, add one more key next to the existing ones:
   `"posts": {{toJSON(<that module>)}}`, or map the module's `elements` array. Either LinkedIn's own shape
   (`share` + `totalShareStatistics`) or a flat one (`urn`, `impressions`, `clicks`, ...) is read.
3. Run the scenario once and check the output bundle: field names in Make can differ slightly from the API's.

Until then the attached file says the scenario doesn't answer per post yet. Post URNs are kept in the stats history
(`li.post_ids`, numbers in `li.post_stats`); `[linkedin] seed_posts` holds the ones from before 27 Nov.
