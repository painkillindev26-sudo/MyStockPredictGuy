# Free online setup (GitHub Actions, no server)

You need **at least one** posting channel. Pinterest is optional; add it whenever it's approved.

## 1. Pick channels you can set up today (all free, no approval wait)
- **Bluesky**: Settings > Privacy and security > App passwords > Add. Secrets: `BLUESKY_HANDLE` (e.g. you.bsky.social), `BLUESKY_APP_PASSWORD`. Posts images only (no video yet).
- **Mastodon**: pick an instance (e.g. mastodon.social), then Preferences > Development > New application, tick `write:statuses` and `write:media`, copy the access token. Secrets: `MASTODON_URL` (e.g. https://mastodon.social), `MASTODON_TOKEN`. Posts images and video.
- **Telegram**: message @BotFather, send /newbot, copy the token. Create a public channel, add your bot as an admin. Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (the channel name, like @yourchannel). Posts images and video.
- **Pinterest (later)**: `PINTEREST_TOKEN`, `PINTEREST_BOARD_ID` once your developer app is approved.

## 2. Set up the repo
1. Make a **PRIVATE** GitHub repo (e.g. `ad-agent`).
2. Add the files (Add file > Create new file / Upload files): `ad_agent.py`, `requirements.txt`, `.gitignore`, `SETUP.md`, and `.github/workflows/agent.yml` (type that full path as the file name; slashes make the folders).
3. Settings > Secrets and variables > Actions > New repository secret. Always add:
   - `ANTHROPIC_API_KEY` (console.anthropic.com; set a low monthly spend limit)
   - `GUMROAD_TOKEN` (Gumroad > Settings > Advanced > Applications)
   - `GH_PAT` (GitHub > Settings > Developer settings > fine-grained token, read-only on your repos)
   - plus the channel secrets from step 1.

## 3. First run (safe mode, posts nothing)
Actions tab > ad-agent > Run workflow. Then open `data/preview.md` in the repo to read every post and video it wants to publish.

## 4. Go live
Settings > Secrets and variables > Actions > Variables > New variable: `LIVE` = `1`.
It then runs by itself: daily plan, up to 3 posts a day spaced an hour apart, to every channel you configured. Set `LIVE` to anything else to pause it instantly.

## Costs
GitHub Actions is free for private repos up to 2,000 minutes/month. Bluesky, Mastodon, Telegram, and Pinterest are free. The Anthropic API is the one paid piece (usage-based), so set a spend limit.
Rendered videos are saved as a downloadable "videos" file on each run (for TikTok / Reels / Shorts, which you post by hand).
