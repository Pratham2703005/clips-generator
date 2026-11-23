# linkedin-publisher

The day's post on the LinkedIn Page "Isha - Job Shorts" (https://www.linkedin.com/company/144151724/), through Make.com.

```
python tools/linkedin-publisher/publish.py --folder content/<date> [--dry-run]
```

- **Post:** the reel's lesson (the description's tips block), the YouTube Short's link, the day's first 2 jobs
  (`daily-jobs.json`, written by the Telegram jobs step), the Telegram link and 3 hashtags, with the reel's cover frame
  as the image. Text goes out in LinkedIn's "little text format" (reserved characters escaped, `#tag` as hashtags).
- **Why Make:** LinkedIn's own Community Management API asks for a business email on your own domain. Make's LinkedIn
  app is already approved. Its free plan takes files up to 5 MB, so the post is text + image, not the ~20 MB reel.
- **Make scenario 1 "linkedin-post":** Custom webhook (API key) -> LinkedIn "Create a Company Image Post" (Content =
  `text`, Image = `image_name` + `toBinary(image_base64; base64)`, Visibility Anyone) -> Webhook response
  `{"post_id": "<the post's id>"}`. Must be active ("Immediately as data arrives"); otherwise Make answers "Accepted".
- **Make scenario 2 "linkedin-stats"** (read by tools/report): Custom webhook -> "Get the Company Share Statistics" ->
  Webhook response `{followers, impressions, unique_impressions, clicks, likes, comments, shares, engagement}`.
  Followers are `null` until the Page has followers (the follower modules return nothing for 0 followers, and the
  networkSizes API call fails on how Make encodes the URN in the path).
- **Secrets:** `MAKE_LINKEDIN_POST_WEBHOOK`, `MAKE_LINKEDIN_STATS_WEBHOOK`, `MAKE_LINKEDIN_API_KEY` (repository
  secrets; `.env` on the PC). None of them is ever printed.
- Writes `linkedin_post_id` / `linkedin_url` / `linkedin_posted_at` into `posted.json` and never posts twice.
