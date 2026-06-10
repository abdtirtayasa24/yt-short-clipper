# 🤖 AGENTS.md - AI Developer Guide for YT-Short-Clipper

## 📌 Project Overview

**YT-Short-Clipper** is now operated through **Bot Control Mode**: a FastAPI Combined Bot Server with a Telegram command interface for clipping, scheduling, publishing, and operations on a VPS.

The legacy Tkinter/CustomTkinter desktop GUI has been removed after Bot Control Mode parity review. Do not reintroduce desktop GUI entry points, pages, dialogs, PyInstaller build artifacts, or desktop-only settings surfaces.

## 🏗️ Architecture & Tech Stack

### Core Technology

- **Language**: Python 3.10+
- **Server**: FastAPI + Uvicorn
- **Bot Control**: `python-telegram-bot`
- **Database**: SQLite via SQLAlchemy + Alembic migrations
- **Video Processing**: FFmpeg via subprocess, OpenCV/MediaPipe paths in `clipper_core.py`
- **Downloading**: yt-dlp
- **AI/ML**:
  - **Gemini Text Provider**: highlight finding and shared publishing metadata via `google-genai`
  - **OpenRouter Media Provider**: Caption Transcription and hook TTS
  - **OpenRouter Audio Adapter**: OpenAI SDK may be used only against OpenRouter-compatible audio endpoints

## High-Level Structure

1. **Combined Bot Server**
   - Entry point: `bot_app/main.py` (`create_app`).
   - Owns FastAPI lifespan, database initialization, Telegram bot startup/shutdown, Clip Archive cleanup, and HTTP routes such as health and Public Clip Link downloads.

2. **Telegram Bot Control Mode**
   - Telegram shell: `bot_app/telegram_bot.py`.
   - Authorized Operator commands include `/start`, `/help`, `/status`, `/defaults`, `/sources`, `/clip`, `/schedule`, `/auth`, and `/cancel`.
   - Only the configured Authorized Operator chat may execute commands.

3. **Domain Services**
   - `bot_app/manual_clipping.py`: Manual Clipping Run Logs, highlight review, selected clip processing, metadata generation, publishing attempts, and cooperative cancellation.
   - `bot_app/source_queue.py`: Source Video Queue lifecycle.
   - `bot_app/scheduler.py`: Scheduled Source URL slots and scheduled firing behavior.
   - `bot_app/clip_archive.py`: Clip Archive record creation, Public Clip Link generation, download safety, and retention cleanup.
   - `bot_app/ai_providers.py`: Gemini Text Provider and OpenRouter Audio Adapter.

4. **Core Video Logic**
   - `clipper_core.py`: Existing clipping/video-processing implementation. Treat this as a large legacy core module; make surgical changes and prefer adding Bot Control boundaries around it instead of broad rewrites.

5. **Data & Config**
   - Environment Configuration is `.env`-based and loaded by `bot_app/settings.py`.
   - SQLite schema is managed by Alembic migrations under `alembic/versions/`.
   - Generated clips are stored in the Clip Archive and served through Public Clip Links.
   - Publishing preauthorization files are generated locally with `tools/preauthorize_publishers.py` and copied to the VPS.

## 📂 Key Directories & Files

| Path | Description |
|------|-------------|
| `bot_app/main.py` | Combined Bot Server FastAPI app factory and lifespan. |
| `bot_app/telegram_bot.py` | Authorized Operator Telegram command shell. |
| `bot_app/models.py` | SQLAlchemy domain models. |
| `bot_app/database.py` | Database engine/session/migration helpers. |
| `bot_app/manual_clipping.py` | Manual Clipping workflows, Run Logs, Clip processing, metadata, publishing. |
| `bot_app/clip_archive.py` | Clip Archive records, Public Clip Links, retention cleanup. |
| `bot_app/source_queue.py` | Source Video Queue helpers. |
| `bot_app/scheduler.py` | Scheduled Source URL slot helpers. |
| `bot_app/ai_providers.py` | Gemini/OpenRouter provider adapters. |
| `clipper_core.py` | Legacy core video-processing logic. |
| `alembic/versions/` | Database migrations. |
| `tests/` | Bot Control Mode tests. |
| `tools/preauthorize_publishers.py` | Local browser-based preauthorization for YouTube/TikTok credentials. |
| `docs/PREAUTHORIZATION.md` | Preauthorization setup instructions. |
| `CONTEXT.md` | Domain glossary and terminology. |

## 🔄 Core Workflows

### 1. Manual Clipping

1. Authorized Operator runs `/clip <youtube_url>`.
2. Bot creates a Run Log and asks the Gemini Text Provider for highlight candidates.
3. Bot displays numbered highlights with title, time range, virality score, hook text, and description.
4. Authorized Operator selects candidates with `/clip select <run_id> <numbers...>` or cancels.
5. `/clip process <run_id>` processes selected highlights through the Clipping Queue.
6. Each generated clip is archived, receives shared metadata, optional publish attempts, and a Telegram summary containing Public Clip Links.

### 2. Source Video Queue and Scheduling

1. Authorized Operator adds URLs with `/sources add <url1> [url2 ...]`.
2. `/schedule add daily <HH:MM>` or `/schedule add weekly <weekday> <HH:MM>` creates enabled schedule slots.
3. Scheduled firings consume pending Source Videos at most once and record Run Logs.
4. Empty queue firings are recorded but do not message Telegram.

### 3. Clip Archive and Public Clip Links

1. Clip records store unguessable IDs, archive paths, Public Clip Links, metadata, expiry, and deletion state.
2. `GET /clips/{clip_id}/download` serves only valid, existing, unexpired, undeleted files under the Clip Archive root.
3. Cleanup removes expired Clip Archive files while preserving Run Log history.

### 4. Publishing

1. Run local preauthorization with `tools/preauthorize_publishers.py` on a machine with a browser.
2. Copy generated credential/session files to the VPS.
3. `/auth` reports YouTube/TikTok preauthorization status.
4. Publishing attempts are recorded per clip/platform when Workflow Defaults enable publishing.

## 🛠️ Development Setup

### Requirements

- Python 3.10+
- FFmpeg and yt-dlp available in PATH or configured for the runtime environment
- Dependencies from `requirements.txt`

### Running Locally

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn bot_app.main:create_app --factory --host 0.0.0.0 --port 8000
```

### Publishing Preauthorization

```bash
python tools/preauthorize_publishers.py --skip-tiktok --youtube-client-secret client_secret.json
python tools/preauthorize_publishers.py --skip-youtube --tiktok-client-key "<client-key>" --tiktok-client-secret "<client-secret>"
```

See `docs/PREAUTHORIZATION.md`.

## 📝 Coding Standards & Conventions

- Use Bot Control Mode domain terms from `CONTEXT.md`.
- Prefer small, focused changes and preserve existing contracts.
- Add Alembic migrations for model/schema changes.
- Keep Telegram commands behind the Authorized Operator guard.
- Do not log secrets, tokens, credentials, raw cookies, or private payloads.
- Environment Configuration belongs in `.env`/`bot_app/settings.py`; runtime Workflow Defaults belong in SQLite.
- For video processing, avoid broad rewrites of `clipper_core.py`; prefer adapters/services around it.
- Avoid reintroducing Tkinter/CustomTkinter, desktop pages/dialogs, `app.py`, pywebview, or PyInstaller build specs.

## ✅ Testing and Verification

Preferred checks:

```bash
pytest -q
python -m py_compile bot_app/*.py tools/preauthorize_publishers.py clipper_core.py youtube_uploader.py tiktok_uploader.py utils/*.py
```

When changing migrations, verify initialization through existing tests or a temporary SQLite database.

## 🤖 AI Agent Tips

- Read `CONTEXT.md` and relevant ADRs before implementing Bot Control Mode changes.
- `tests/test_combined_bot_server.py` covers most Bot Control workflows.
- `tests/test_bot_control_mode_parity.py` prevents legacy desktop GUI artifacts from returning.
- If adding Telegram behavior, test handlers with fake updates/contexts rather than live Telegram API calls.
- If adding video or publishing behavior, use fake processors/publishers in tests; do not call FFmpeg, Gemini, OpenRouter, YouTube, or TikTok in unit tests.

## Agent Skills

### Issue tracker

Issues are tracked in GitHub Issues for this repo. See `docs/agents/issue-tracker.md`.

### Triage labels

Triage labels use the default canonical vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

Domain docs use a single-context layout. See `docs/agents/domain.md`.

## 🔗 Related Documentation

- `README.md`: Bot Control Mode setup and operation.
- `CONTEXT.md`: Domain glossary.
- `docs/PREAUTHORIZATION.md`: YouTube/TikTok preauthorization setup.
- `docs/adr/`: Architecture decisions.
