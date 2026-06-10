# Publishing Preauthorization Setup

Run this on your local machine first, where a browser is available. Then copy the generated files to the VPS.

## YouTube

Place your Google OAuth `client_secret.json` in the repo root, then run:

```bash
python tools/preauthorize_publishers.py --skip-tiktok --youtube-client-secret client_secret.json
```

This writes:

```text
credentials/youtube.json
```

## TikTok

Run:

```bash
python tools/preauthorize_publishers.py \
  --skip-youtube \
  --tiktok-client-key "<client-key>" \
  --tiktok-client-secret "<client-secret>" \
  --tiktok-mode sandbox
```

This writes:

```text
credentials/tiktok.session
```

Use `--tiktok-mode production` for a production TikTok app.

## Copy to VPS

Copy the generated files to the VPS and set matching environment variables:

```env
YOUTUBE_CREDENTIALS_PATH=credentials/youtube.json
TIKTOK_SESSION_PATH=credentials/tiktok.session
```

Then use `/auth` in Telegram to confirm Bot Control Mode can see them.
