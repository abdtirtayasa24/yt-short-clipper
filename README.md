# YT-Short-Clipper

Automated YouTube-to-short-form clipping for VPS deployment through **Bot Control Mode**.

Bot Control Mode replaces the legacy desktop GUI with a Combined Bot Server: one FastAPI process that serves Public Clip Links while running the Telegram bot, scheduler, Clipping Queue, Run Logs, Clip Archive retention cleanup, and Publishing workflows.

## What it does

- Accepts Source Videos from the Authorized Operator in Telegram.
- Stores Workflow Defaults in SQLite and lets Telegram update them.
- Finds Manual Clipping highlights with the Gemini Text Provider.
- Processes selected highlights through a single-active-run Clipping Queue.
- Generates Clip Archive files and unguessable Public Clip Links.
- Generates shared publishing metadata for every clip.
- Records YouTube/TikTok PublishAttempts using preauthorized credentials.
- Supports daily/weekly Scheduled Source URL time slots.
- Cleans up expired Clip Archive files while preserving Run Log history.

## Run Bot Control Mode

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and fill in the required Environment Configuration:

```bash
cp .env.example .env
```

3. Start the Combined Bot Server:

```bash
uvicorn bot_app.main:create_app --factory --host 0.0.0.0 --port 8000
```

4. In Telegram, use `/start`, `/help`, `/status`, `/defaults`, `/sources`, `/clip`, `/schedule`, `/auth`, and `/cancel` as the Authorized Operator.

## Publishing preauthorization

YouTube and TikTok publishing require one-time local browser authorization before files are copied to the VPS.

See [`docs/PREAUTHORIZATION.md`](docs/PREAUTHORIZATION.md).

Generated files default to:

```text
credentials/youtube.json
credentials/tiktok.session
```

Configure matching paths in `.env`:

```env
YOUTUBE_CREDENTIALS_PATH=credentials/youtube.json
TIKTOK_SESSION_PATH=credentials/tiktok.session
```

Use `/auth` in Telegram to verify the VPS can see them.

## Project structure

```text
bot_app/                    Bot Control Mode application code
alembic/                    SQLite migrations
clipper_core.py             Core clipping/video processing behavior
tools/preauthorize_publishers.py
                            Local publishing preauthorization helper
tests/                      Bot Control Mode tests
```

## Test

```bash
pytest -q
```

## Notes

- The legacy Tkinter/CustomTkinter desktop GUI has been removed after Bot Control Mode parity review.
- The application is now operated through FastAPI and Telegram.
- Treat `.env`, credentials, cookies, generated clips, and publishing session files as sensitive.

## License

MIT — see [`LICENSE`](LICENSE).
