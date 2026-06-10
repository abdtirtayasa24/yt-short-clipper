# Publishing Preauthorization Setup

Run this on your local machine first, where a browser is available. Then copy the generated files to the VPS.

## YouTube

Create a Google OAuth client for YouTube upload. The recommended client type is **Desktop app**.

Place the downloaded Google OAuth `client_secret.json` in the repo root, then run:

```bash
python tools/preauthorize_publishers.py --skip-tiktok --youtube-client-secret client_secret.json
```

If you use a Google OAuth **Web application** client instead of Desktop app, add this exact authorized redirect URI in Google Cloud Console before running the command:

```text
http://localhost:8080/
```

Or choose another port and pass `--youtube-oauth-port <port>` while adding the matching `http://localhost:<port>/` redirect URI.

This writes:

```text
credentials/youtube.json
```

## TikTok

In TikTok Developer Console, configure this exact redirect URI:

```text
http://localhost:8080/callback
```

Then run:

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

Use `--tiktok-mode production` for a production TikTok app. If your TikTok app uses a different local redirect URI, pass `--tiktok-redirect-uri "<exact-uri>"` and make sure the same URI is configured in TikTok Developer Console.

## Common "Access blocked" / "Invalid request" causes

- **Google YouTube:** `client_secret.json` is a Web application client and `http://localhost:8080/` is not listed as an authorized redirect URI. Use a Desktop app client, or add the exact localhost redirect.
- **Google YouTube:** OAuth consent screen is still in Testing and your Google account is not added as a test user.
- **TikTok:** the redirect URI in TikTok Developer Console does not exactly match `http://localhost:8080/callback` or the value passed with `--tiktok-redirect-uri`.
- **TikTok:** the app does not have the required scopes approved/enabled: `user.info.basic`, `video.upload`, and `video.publish`.

## Copy to VPS

Copy the generated files to the VPS and set matching environment variables:

```env
YOUTUBE_CREDENTIALS_PATH=credentials/youtube.json
TIKTOK_SESSION_PATH=credentials/tiktok.session
```

Then use `/auth` in Telegram to confirm Bot Control Mode can see them.
