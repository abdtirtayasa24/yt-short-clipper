# YT Short Clipper

YT Short Clipper turns long-form YouTube videos into short-form clips and is moving from a desktop-controlled workflow toward Telegram-controlled automation.

## Language

**Bot Control Mode**:
A Telegram-driven operating mode where users control clipping, publishing, configuration, and automation without the desktop GUI.
_Avoid_: Telegram GUI, bot Telegram control, headless GUI

**Publishing**:
Sending a generated clip to an external platform such as YouTube, TikTok, or Telegram.
_Avoid_: Uploading when the destination is unclear

**Environment Configuration**:
The non-interactive `.env`-based source of secrets and operational settings for Bot Control Mode.
_Avoid_: API key settings screen, Telegram API key form

**Preauthorization Setup**:
A one-time interactive OAuth setup performed locally before Bot Control Mode scheduler jobs can publish to YouTube or TikTok on a VPS.
_Avoid_: Headless OAuth, scheduler login

**Authorized Operator**:
The single Telegram chat allowed to control Bot Control Mode and receive generated clips.
_Avoid_: User, admin, allowed users

**Command-First Hybrid Interface**:
A Telegram interaction style where slash commands start workflows and inline buttons collect workflow choices.
_Avoid_: Chatbot UI, menu-only bot

**Manual Clipping**:
A Bot Control Mode workflow where the Authorized Operator reviews detected highlights before processing selected clips.
_Avoid_: One-shot clipping

**Source Video**:
A YouTube video URL submitted by the Authorized Operator as input for manual or scheduled clipping.
_Avoid_: Previous URL, old URL, link when lifecycle matters

**Source Video Queue**:
A pending list of Source Videos consumed one by one by scheduled runs without reusing completed videos.
_Avoid_: Recurring URL, implicit URL reuse

**Scheduled Source URL**:
A scheduler workflow that consumes Source Videos from the Source Video Queue at configured times.
_Avoid_: Auto-search job, implicit URL reuse

**Scheduled Discovery**:
A scheduler workflow that searches for candidate YouTube videos automatically before clipping them.
_Avoid_: Viral scheduler when discovery criteria are unclear

**Scheduled Clip**:
The highest-ranked generated clip produced by a scheduled run unless the job explicitly asks for more clips.
_Avoid_: Scheduled batch when only one clip is expected

**Clip Archive**:
The VPS-hosted storage area for generated clip files that can be downloaded by URL and retained for a configured number of days.
_Avoid_: Output folder when the file must be web-downloadable

**Run Log**:
A database record of a manual or scheduled clipping run, including source video, generated clips, status, and errors.
_Avoid_: Console log when persistence is required

**Workflow Defaults**:
Telegram-configurable SQLite settings that define default clipping options such as captions, hooks, clip counts, and publishing destinations.
_Avoid_: Environment defaults when the setting should change at runtime

**Public Clip Link**:
An unguessable FastAPI-served URL that allows anyone with the link to download a generated clip and is the only clip delivery method used by Telegram.
_Avoid_: Authenticated clip link, predictable file URL, Telegram video upload

**Combined Bot Server**:
A single FastAPI process that serves clip downloads while running the Telegram bot and scheduler.
_Avoid_: Separate worker architecture for v1

**Clipping Queue**:
A persistent queue that allows only one active clipping run at a time while holding pending manual and scheduled runs.
_Avoid_: Parallel clipping runs

**Gemini Text Provider**:
The only text-generation provider used for highlight finding and YouTube title making.
_Avoid_: OpenAI-compatible highlight provider, multi-provider text settings

**OpenRouter Media Provider**:
The only media-generation provider used for caption transcription and hook voice generation through OpenRouter audio endpoints.
_Avoid_: OpenAI Whisper provider, OpenAI TTS provider

**OpenRouter Audio Adapter**:
The OpenAI Python SDK used only as a compatibility adapter for OpenRouter speech-to-text and text-to-speech endpoints.
_Avoid_: OpenAI provider, OpenAI API dependency

**Caption Transcription**:
The OpenRouter speech-to-text step that creates transcript text and timestamps from clip audio.
_Avoid_: Caption maker when referring only to transcription

**Caption Rendering**:
The local FFmpeg/ASS step that burns styled subtitles into a generated clip.
_Avoid_: Caption maker when referring only to rendering

## Relationships

- **Bot Control Mode** will eventually replace the desktop-controlled workflow.
- A **Run Log** records the source YouTube video link and produced **Clip Archive** links.
- A **Scheduled Clip** is produced by a scheduled run and stored in the **Clip Archive**.
- A **Public Clip Link** is built from the server's configured public base URL.
- **Environment Configuration** is the only place to change AI provider keys and model names.

## Example dialogue

> **Dev:** "Should this setting remain in the desktop app?"
> **Domain expert:** "No — once **Bot Control Mode** reaches parity, Telegram becomes the control surface."

## Flagged ambiguities

- "Bot Telegram control" was resolved to **Bot Control Mode**.
