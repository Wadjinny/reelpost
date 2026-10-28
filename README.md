# reelpost

One command publishes a Reel to Instagram, a Facebook Page, and YouTube Shorts. The same description, hashtags, and thumbnail go to each one.

```bash
reelpost publish clip.mp4 \
  --description "Morning on the canal" \
  --tags berlin,travel \
  --thumbnail cover.jpg \
  --yes
```

The default is all three. `--to instagram`, `--to facebook`, or `--to youtube` posts to one. `--to both` is Instagram and Facebook. `--to all` is the default set.

## What you need

- A Meta app with Facebook Login. In development mode, only people with a role on the app can log in. For your own Page, add yourself as an app admin and App Review is not required.
- A Facebook Page you can create content on.
- An Instagram professional account (Business or Creator) linked to that Page, if you want Instagram.
- The video on this machine. Personal Facebook profiles cannot publish Reels through the API.
- For YouTube, a Google Cloud project with the YouTube Data API v3 enabled, and a channel. Custom thumbnails need a phone-verified channel.

Add this redirect URI in the app, under Facebook Login → Settings → Valid OAuth Redirect URIs:

```text
http://localhost:53682/callback
```

Add these permissions to the login configuration:

- `pages_show_list`
- `pages_read_engagement`
- `pages_manage_posts`
- `pages_manage_engagement`
- `pages_read_user_content`
- `instagram_basic`
- `instagram_content_publish`
- `business_management`

If the Page role was granted through Business Manager, also add `ads_management` and `ads_read` on the login configuration. Facebook Login for Business grants nothing unless every requested permission is accepted.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Copy `.env.example` to `.env` if you want the app id and secret loaded from the current directory.

## Log in

```bash
export REELPOST_APP_ID=your-app-id
export REELPOST_APP_SECRET=your-app-secret
reelpost auth login
```

If the app uses a Facebook Login for Business configuration, pass `--config-id`. The browser opens Facebook. After you approve access, reelpost stores a Page token in `~/.config/reelpost/config.json` (mode `0600`). The app secret is not saved.

```bash
reelpost accounts
reelpost auth status
```

`reelpost auth logout` deletes that file, including a saved YouTube login. `reelpost auth logout --youtube` removes only YouTube.

## YouTube

YouTube Shorts use a separate Google login. In Google Cloud, create an OAuth client of type Desktop, enable YouTube Data API v3, and add this redirect URI:

```text
http://localhost:53683/callback
```

While the app is in testing, add the Google account as a test user. The login asks for `youtube.upload` and `youtube.readonly`.

```bash
export REELPOST_YOUTUBE_CLIENT_ID=your-client-id
export REELPOST_YOUTUBE_CLIENT_SECRET=your-client-secret
reelpost auth youtube
```

The client secret is not saved. Leave it in the environment so the refresh token can be exchanged later. `reelpost auth login` keeps the YouTube login, and `reelpost auth youtube` keeps the Page login.

```bash
reelpost publish clip.mp4 --to youtube --privacy unlisted --thumbnail cover.jpg --yes
```

`--privacy` is `public` (the default), `unlisted`, or `private`. It applies to YouTube only. A scheduled Short is private until the chosen time, then becomes public, so `--privacy unlisted` cannot be combined with `--schedule`.

YouTube requires a title of at most 100 characters. Without `--title`, reelpost uses the first line of the description. `#Shorts` is added to the YouTube description when it is not already there. It is not added to the Instagram or Facebook caption. Keyword tags are the same words, up to 500 characters.

The default Data API quota is 10,000 units a day, and each video upload costs 1,600, so about 6 uploads a day until Google raises the quota. A 403 on the cover usually means the channel has no verified phone number. Retry it with:

```bash
reelpost thumbnail VIDEO_ID --to youtube --thumbnail cover.jpg --yes
```

You can skip login and set a Page token yourself:

```bash
export REELPOST_PAGE_ID=...
export REELPOST_PAGE_TOKEN=...
export REELPOST_IG_USER_ID=...
```

## Publish

```bash
reelpost publish clip.mp4 \
  --description "Morning on the canal" \
  --description-file caption.txt \
  --tags berlin,travel \
  --thumbnail cover.jpg \
  --title "Morning canal" \
  --yes
```

Pass either `--description` or `--description-file`. Tags can be comma-separated or repeated, with or without `#`. Tags already written in the description are not added twice. The limit is 30 hashtags and 2200 characters.

`--title` is sent to YouTube and Facebook. Without it, YouTube uses the first line of the description, shortened to 100 characters. `--collaborators natgeo,someuser` invites up to three public Instagram accounts. `--place-id` tags a location Page. `--ai-generated` sets Meta’s AI label and YouTube’s synthetic-media flag. `--reels-tab-only` keeps the Instagram Reel off the profile feed. `--privacy` is YouTube only.

Check a file without uploading:

```bash
reelpost check clip.mp4 --thumbnail cover.jpg --tags berlin
```

`--dry-run` prints the same plan, including the saved Page name, and does not upload.

### Thumbnail

- Facebook and YouTube receive the image file as the cover.
- Instagram cannot take a local file. Meta downloads `cover_url` itself. A local `--thumbnail` is served through a short-lived `cloudflared` tunnel (`https://*.trycloudflare.com`) until processing finishes. The video does not go through that tunnel. The cover image is briefly public at a random URL.
- `--thumbnail-url https://...` skips the tunnel when the URL is already a public JPEG. It must be `https`. YouTube still downloads that image and uploads it.
- `--thumb-offset-ms 1500` picks an Instagram frame when you have no image. Facebook and YouTube still need `--thumbnail` if you want a custom cover.

`cloudflared` is required only for a local Instagram cover. It is already installed on this machine.

JPEG, PNG, and WebP are accepted. PNG and WebP are converted to JPEG. The file is kept small enough for every selected platform: 8 MB for Instagram, 10 MB for Facebook, and 2 MB for YouTube. With all three selected, that means 2 MB. A 9:16 image avoids cropping.

### Facebook schedule

Instagram cannot be scheduled. Facebook and YouTube can, together or alone:

```bash
reelpost publish clip.mp4 \
  --to facebook,youtube \
  --schedule 2026-10-29T09:00+01:00 \
  --description "Tomorrow" \
  --thumbnail cover.jpg \
  --yes
```

A time with no zone uses this computer’s local timezone. The slot has to be at least 10 minutes ahead and within 6 months.

### One platform fails

If a selected account is not logged in, nothing is published. After that, Facebook is attempted first, then Instagram, then YouTube. If one upload fails, the others still run. The command exits with status 1 when any side fails. A cover that fails after the video is published can be retried:

```bash
reelpost thumbnail VIDEO_ID --thumbnail cover.jpg --yes
reelpost thumbnail VIDEO_ID --to youtube --thumbnail cover.jpg --yes
```

## Video rules

The command checks these before it uploads. If any selected target would reject the file, nothing is published.

| | Instagram | Facebook Page | YouTube Shorts |
| --- | --- | --- | --- |
| Length | 3 seconds–15 minutes | 3–90 seconds | 1 second–3 minutes |
| File size | 300 MB | | |
| Picture | moov atom at the front of the file | at least 540×960, aspect from 9:16 to 16:9 | vertical or square |
| Recommended | 1080×1920, H.264 or HEVC, AAC | 1080×1920, 24–60 fps | 1080×1920 |

If Instagram complains about the moov atom:

```bash
ffmpeg -i input.mp4 -c copy -movflags +faststart output.mp4
```

Instagram allows 50 API posts per account per rolling 24 hours. Facebook allows 30. reelpost reads the Instagram quota before uploading and stops if it is used up.

## Tests

```bash
python -m unittest discover -s tests -v
```
