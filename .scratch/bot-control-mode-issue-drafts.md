# Bot Control Mode issue drafts

These drafts are ready to publish to GitHub Issues with the `needs-triage` label once the `gh` CLI is available.

## 1. Bootstrap Combined Bot Server with database-backed Workflow Defaults

## What to build

Add the first runnable Combined Bot Server slice: a FastAPI app that loads Environment Configuration, initializes SQLAlchemy/Alembic-backed SQLite storage, exposes a health check, and creates the initial Workflow Defaults row.

## Acceptance criteria

- [ ] `bot_app` can be started with Uvicorn and responds successfully to `GET /health`.
- [ ] Environment Configuration is loaded from `.env` with typed validation for required static settings.
- [ ] SQLAlchemy models and Alembic setup exist for SQLite, with a path that can later support Postgres/Supabase.
- [ ] Initial Workflow Defaults are created in SQLite with captions on, hooks on, YouTube/TikTok publishing off, English subtitle preference, and agreed clip counts.

## Blocked by

None - can start immediately

---

## 2. Serve Clip Archive files through Public Clip Links

## What to build

Add Clip Archive storage and a FastAPI download route that serves generated clip files through unguessable Public Clip Links built from `PUBLIC_BASE_URL`.

## Acceptance criteria

- [ ] Clip records can store an unguessable clip identifier, local archive path, and Public Clip Link.
- [ ] `GET /clips/{clip_id}/download` serves only existing clip files recorded in the database.
- [ ] The route rejects missing, expired, or path-traversal attempts safely.
- [ ] Public Clip Links are generated from configured `PUBLIC_BASE_URL`.

## Blocked by

- Issue 1

---

## 3. Replace selectable AI providers with Gemini Text Provider and OpenRouter Audio Adapter

## What to build

Replace selectable AI provider behavior with fixed task-specific providers: Gemini Text Provider for highlight finding and shared metadata, and OpenRouter Media Provider for Caption Transcription and hook voice generation.

## Acceptance criteria

- [ ] Gemini text calls use `google-genai` and `.env` models `gemini-3.1-flash-lite` by default.
- [ ] OpenRouter Caption Transcription uses the OpenRouter API key and `openai/whisper-1` by default.
- [ ] OpenRouter hook TTS uses `canopylabs/orpheus-3b-0.1-ft` with voice `josh` by default.
- [ ] OpenAI provider keys/settings are not required or exposed as selectable providers.
- [ ] The OpenAI Python SDK, if used, is only pointed at OpenRouter as the OpenRouter Audio Adapter.

## Blocked by

- Issue 1

---

## 4. Add Authorized Operator Telegram bot shell

## What to build

Add a Telegram bot shell inside the Combined Bot Server that only responds to the Authorized Operator chat configured in Environment Configuration.

## Acceptance criteria

- [ ] FastAPI lifespan starts and stops the Telegram bot cleanly.
- [ ] Unknown Telegram chat IDs are rejected without running commands.
- [ ] `/start`, `/help`, and `/status` work for the Authorized Operator.
- [ ] Bot startup fails fast when required Telegram Environment Configuration is missing.

## Blocked by

- Issue 1

---

## 5. Manage Workflow Defaults from Telegram

## What to build

Allow the Authorized Operator to view and update Workflow Defaults stored in SQLite from Telegram commands.

## Acceptance criteria

- [ ] `/defaults` displays current Workflow Defaults.
- [ ] `/defaults set captions on|off` updates the stored default.
- [ ] `/defaults set hooks on|off` updates the stored default.
- [ ] `/defaults set publish_youtube on|off` and `/defaults set publish_tiktok on|off` update stored Publishing defaults.
- [ ] `/defaults set subtitle_language <code>` updates the preferred subtitle language.

## Blocked by

- Issue 4

---

## 6. Manage Source Video Queue from Telegram

## What to build

Add Source Video Queue commands so the Authorized Operator can submit multiple YouTube URLs for scheduled processing without reusing completed Source Videos.

## Acceptance criteria

- [ ] `/sources add <url1> [url2 ...]` stores each URL as a pending Source Video.
- [ ] `/sources list` shows pending, consumed, and failed Source Videos clearly.
- [ ] `/sources remove <source_id>` removes or cancels a pending Source Video.
- [ ] Completed Source Videos are marked consumed and are never reused by scheduled runs.

## Blocked by

- Issue 4

---

## 7. Run Manual Clipping highlight review from Telegram

## What to build

Implement Manual Clipping through Telegram: `/clip <url>` creates a Run Log, finds highlights with the Gemini Text Provider, and lets the Authorized Operator review/select highlights.

## Acceptance criteria

- [ ] `/clip <youtube_url>` starts Manual Clipping for the Authorized Operator.
- [ ] The bot finds the configured number of highlight candidates using current Workflow Defaults.
- [ ] Telegram displays numbered highlights with title, time range, virality score, hook text, and description.
- [ ] The Authorized Operator can select highlights or cancel before processing.
- [ ] Run Log and RunEvent records capture progress and errors.

## Blocked by

- Issue 3
- Issue 4
- Issue 5

---

## 8. Process selected Manual Clipping highlights into Public Clip Links

## What to build

Process selected Manual Clipping highlights through the Clipping Queue, generate final clips with Caption Rendering and hooks according to Workflow Defaults, archive them, and send Public Clip Links to Telegram.

## Acceptance criteria

- [ ] Only one clipping run is active at a time through the Clipping Queue.
- [ ] Selected highlights are processed into final clip files using existing clipping behavior.
- [ ] Captions and hooks follow Workflow Defaults and manual overrides where present.
- [ ] Each generated clip is stored in the Clip Archive and recorded in SQLite.
- [ ] Telegram sends only Public Clip Links, not uploaded video files.

## Blocked by

- Issue 2
- Issue 7

---

## 9. Generate shared publishing metadata for every clip

## What to build

Generate one shared metadata set for every generated clip using the Gemini Text Provider, even when YouTube/TikTok Publishing is disabled.

## Acceptance criteria

- [ ] Every generated clip gets a shared generated title, description, and hashtags.
- [ ] Metadata generation uses the configured Gemini YouTube title model.
- [ ] Generated metadata is stored on the Clip record.
- [ ] Telegram run summaries include the generated metadata title.

## Blocked by

- Issue 3
- Issue 8

---

## 10. Add Scheduled Source URL time slots

## What to build

Add daily and weekly scheduled time slots that consume one pending Source Video per run, find configured highlight candidates, process the top Scheduled Clip, and stay silent when the queue is empty.

## Acceptance criteria

- [ ] `/schedule add daily <HH:MM>` creates an enabled daily schedule in `Asia/Jakarta` by default.
- [ ] `/schedule add weekly <weekday> <HH:MM>` creates an enabled weekly schedule.
- [ ] `/schedule list` and `/schedule remove <schedule_id>` manage schedules.
- [ ] Each scheduled firing consumes at most one pending Source Video by default.
- [ ] Empty Source Video Queue firings are recorded in the Run Log but do not message Telegram.

## Blocked by

- Issue 6
- Issue 8

---

## 11. Integrate YouTube and TikTok Publishing with Preauthorization Setup

## What to build

Connect generated clips to existing YouTube and TikTok uploaders using Preauthorization Setup, record PublishAttempts, and report platform URLs or errors in Telegram summaries.

## Acceptance criteria

- [ ] `/auth` shows YouTube and TikTok preauthorization status and setup instructions for VPS deployment.
- [ ] Publishing uses stored/preauthorized YouTube and TikTok credentials, not interactive OAuth during scheduled jobs.
- [ ] PublishAttempt records are created per clip/platform with status, URL, and error details.
- [ ] Publishing obeys Workflow Defaults and per-run overrides where present.
- [ ] Telegram summaries include YouTube/TikTok publish status and URLs when available.

## Blocked by

- Issue 8
- Issue 9

---

## 12. Add Bot Control Mode operational controls

## What to build

Add operational controls for the Authorized Operator to inspect active/queued work and request cooperative cancellation.

## Acceptance criteria

- [ ] `/status` shows active run, queued runs, recent Run Log state, and Source Video Queue summary.
- [ ] `/cancel` requests cooperative cancellation for the active run or removes queued work where applicable.
- [ ] Cancelled runs are marked clearly in Run Log and RunEvent records.
- [ ] The worker checks cancellation between major clipping steps and avoids starting new work after cancellation.

## Blocked by

- Issue 8
- Issue 10

---

## 13. Add Clip Archive retention cleanup

## What to build

Add cleanup for expired Clip Archive files based on the configured retention policy, defaulting to 30 days, while preserving Run Log history.

## Acceptance criteria

- [ ] Clip Archive retention defaults to 30 days and is configurable.
- [ ] Cleanup removes expired clip files without deleting Run Log history.
- [ ] Clip records reflect deleted/expired archive state.
- [ ] Cleanup runs safely inside the Combined Bot Server without blocking clipping work.

## Blocked by

- Issue 2
- Issue 10

---

## 14. Remove Tkinter desktop GUI after Bot Control Mode parity

## What to build

After Bot Control Mode reaches parity, remove the Tkinter desktop GUI and desktop-only settings/pages/build artifacts so the project runs through the Combined Bot Server.

## Acceptance criteria

- [ ] Human parity review confirms Bot Control Mode covers clipping, scheduling, defaults, Public Clip Links, Run Logs, and YouTube/TikTok Publishing.
- [ ] Tkinter/CustomTkinter pages, dialogs, and desktop entry points are removed or archived as agreed.
- [ ] Desktop-only dependencies and build specs are removed when no longer needed.
- [ ] Documentation points users to FastAPI/Telegram deployment instead of desktop startup.

## Blocked by

- Issues 4 through 13
