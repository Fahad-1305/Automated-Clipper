<<<<<<< HEAD
# ClipBot Dashboard

Local web app: manage streamers, connect YouTube / Facebook / Instagram, and run or schedule clip posting.

## Run
1. Install `ffmpeg` and `yt-dlp`, then `pip install -r requirements.txt`
2. `uvicorn app:app --host 127.0.0.1 --port 8000`
3. Open http://localhost:8000

## First-time setup (Accounts tab)
- **Twitch**: create an app at dev.twitch.tv/console, paste Client ID and secret.
- **YouTube**: Google Cloud project, enable YouTube Data API v3, create an OAuth *Web* client,
  add the redirect URI shown in the app, paste ID and secret, click Connect YouTube.
- **Facebook + Instagram**: Meta developer app, add the redirect URI shown in the app. The Instagram
  account must be Business/Creator and linked to your Page. Click Connect, then pick the Page.
- Credentials are stored in `clipbot.db` on your machine. Keep it private and don't expose the app
  to the internet: it has no login.

## Using it
- **Streamers tab**: add Twitch usernames and tick "Permission confirmed" only after the streamer agrees.
  Unticked streamers are never clipped.
- **Settings tab**: game filter, min views, clip length, platforms, YouTube visibility, auto-run interval.
- **Dashboard**: "Run now" or enable auto-run; the activity log and recent posts update live.

## Notes
- Auto-run only works while the app is running. For 24/7 use, host it on a small VPS behind a login or VPN.
- YouTube forces uploads to private until Google audits your API project.
- Instagram Reels posting uses resumable upload; in development mode it works for your own account.
=======
# Automated-Clipper
An automated video clipping and social media publishing platform built with Python. Collects Twitch clips, processes video content, and streamlines publishing across YouTube, Instagram, and Facebook.
>>>>>>> 787480adee5ab86b8a3de50bfaebc036a6a47cdb
