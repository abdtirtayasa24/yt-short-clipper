from typing import Protocol

from sqlalchemy import func, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot_app.database import create_session_factory, ensure_workflow_defaults, initialize_database
from bot_app.manual_clipping import ManualClippingService, TelegramVideoUpload
from bot_app.models import RunLog, SourceVideo
from bot_app.scheduler import add_daily_schedule, add_weekly_schedule, delete_schedule, list_schedules, remove_schedule, set_schedule_enabled
from bot_app.settings import Settings
from bot_app.source_queue import add_source_videos, cancel_pending_source_video, get_source_videos


class TelegramBotShell(Protocol):
    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...


class AuthorizedOperatorTelegramBot:
    """Command-first Telegram shell restricted to the Authorized Operator."""

    def __init__(self, settings: Settings, manual_clipping_service: ManualClippingService | None = None):
        self.settings = settings
        initialize_database(settings.database_url)
        self.session_factory = create_session_factory(settings.database_url)
        self.manual_clipping_service = manual_clipping_service or ManualClippingService(settings)
        self.application: Application | None = None
        self.started = False

    def validate_configuration(self) -> None:
        if not self.settings.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        if self.settings.telegram_authorized_chat_id is None:
            raise ValueError("TELEGRAM_AUTHORIZED_CHAT_ID is required")

    async def start(self) -> None:
        self.validate_configuration()
        self.application = ApplicationBuilder().token(self.settings.telegram_bot_token).build()
        self.application.add_handler(CommandHandler("start", self.handle_start))
        self.application.add_handler(CommandHandler("help", self.handle_help))
        self.application.add_handler(CommandHandler("status", self.handle_status))
        self.application.add_handler(CommandHandler("defaults", self.handle_defaults))
        self.application.add_handler(CommandHandler("sources", self.handle_sources))
        self.application.add_handler(CommandHandler("clip", self.handle_clip))
        self.application.add_handler(CommandHandler("schedule", self.handle_schedule))
        self.application.add_handler(CommandHandler("auth", self.handle_auth))
        self.application.add_handler(CommandHandler("cancel", self.handle_cancel))
        self.application.add_handler(CallbackQueryHandler(self.handle_schedule_callback, pattern=r"^schedule:"))
        self.application.add_handler(CallbackQueryHandler(self.handle_sources_callback, pattern=r"^sources:"))
        self.application.add_handler(CallbackQueryHandler(self.handle_defaults_callback, pattern=r"^defaults:"))
        self.application.add_handler(CallbackQueryHandler(self.handle_menu_callback, pattern=r"^menu:"))
        self.application.add_handler(CallbackQueryHandler(self.handle_clip_callback, pattern=r"^clip:"))
        self.application.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, self.handle_video_upload))
        await self.application.initialize()
        if self.application.updater is None:
            raise RuntimeError("Telegram bot requires an updater for polling")
        await self.application.updater.start_polling(drop_pending_updates=True)
        await self.application.start()
        self.started = True

    async def stop(self) -> None:
        if self.application is not None:
            if self.application.updater is not None:
                await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
        self.started = False
        self.application = None

    async def _reject_unknown_chat(self, update: Update) -> bool:
        chat = getattr(update, "effective_chat", None)
        if chat is not None and chat.id == self.settings.telegram_authorized_chat_id:
            return False

        message = getattr(update, "message", None)
        if message is not None:
            await message.reply_text("Unauthorized chat.")
        return True

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False
        await update.message.reply_text(
            "Bot Control Mode is ready. Use /help to see available commands.",
            reply_markup=self._home_menu_markup(),
        )
        return True

    async def handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False
        await update.message.reply_text(
            "Available commands: /start, /help, /status, /defaults, /sources, /clip, /schedule, /auth",
            reply_markup=self._home_menu_markup(),
        )
        return True

    def _home_menu_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("New Clip", callback_data="menu:new_clip")],
                [InlineKeyboardButton("Source Queue", callback_data="menu:sources")],
                [InlineKeyboardButton("Schedule", callback_data="menu:schedule")],
                [InlineKeyboardButton("Workflow Defaults", callback_data="menu:defaults")],
                [InlineKeyboardButton("Status", callback_data="menu:status")],
                [InlineKeyboardButton("Auth", callback_data="menu:auth")],
            ]
        )

    async def handle_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        query = getattr(update, "callback_query", None)
        if query is None:
            return False
        if await self._reject_unknown_chat(update):
            await query.answer("Unauthorized chat.")
            return False
        action = str(getattr(query, "data", "")).split(":", 1)[-1]
        await query.answer()
        if action == "new_clip":
            await query.edit_message_text(
                "Send /clip <youtube_url_or_direct_video_url>, or upload a video file to start Manual Clipping.",
                reply_markup=self._home_menu_markup(),
            )
            return True
        if action == "sources":
            await query.edit_message_text(self._format_source_videos(), reply_markup=self._source_videos_markup())
            return True
        if action == "schedule":
            with self.session_factory() as session:
                text = self._format_schedules(session)
            await query.edit_message_text(text, reply_markup=self._schedules_markup())
            return True
        if action == "defaults":
            await query.edit_message_text(self._format_workflow_defaults(), reply_markup=self._workflow_defaults_markup())
            return True
        if action == "status":
            await query.edit_message_text(self._status_text(), reply_markup=self._home_menu_markup())
            return True
        if action == "auth":
            await query.edit_message_text(self._auth_text(), reply_markup=self._home_menu_markup())
            return True
        await query.edit_message_text("Unknown menu action.", reply_markup=self._home_menu_markup())
        return False

    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False
        await update.message.reply_text(self._status_text())
        return True

    def _status_text(self) -> str:
        with self.session_factory() as session:
            active_run = self.manual_clipping_service.clipping_queue.active_run_id
            queued_runs = session.scalar(select(func.count()).select_from(RunLog).where(RunLog.status == "queued"))
            recent_runs = session.scalars(select(RunLog).order_by(RunLog.id.desc()).limit(5)).all()
            source_counts = {
                status: count
                for status, count in session.execute(
                    select(SourceVideo.status, func.count()).group_by(SourceVideo.status)
                ).all()
            }
        source_summary = ", ".join(f"{status}={count}" for status, count in sorted(source_counts.items())) or "none"
        recent_summary = ", ".join(f"#{run.id} {run.status}" for run in recent_runs) or "none"
        return (
            "Bot Control Mode status: ok\n"
            f"active run: {active_run or 'none'}\n"
            f"queued runs: {queued_runs}\n"
            f"recent runs: {recent_summary}\n"
            f"Source Video Queue: {source_summary}"
        )

    async def handle_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False
        active_run = self.manual_clipping_service.clipping_queue.active_run_id
        with self.session_factory() as session:
            if active_run is not None and self.manual_clipping_service.request_cancellation(session, active_run):
                await update.message.reply_text(f"Cancellation requested for Run Log {active_run}.")
                return True
            queued_run = session.scalars(select(RunLog).where(RunLog.status == "queued").order_by(RunLog.id)).first()
            if queued_run is not None:
                queued_run.status = "cancelled"
                self.manual_clipping_service.add_event(session, queued_run, "cancelled", "Queued run cancelled")
                session.commit()
                await update.message.reply_text(f"Cancelled queued Run Log {queued_run.id}.")
                return True
        await update.message.reply_text("No active or queued run to cancel.")
        return False

    async def handle_auth(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False
        await update.message.reply_text(self._auth_text())
        return True

    def _auth_text(self) -> str:
        youtube_status = "preauthorized" if self.settings.youtube_credentials_path.exists() else "missing"
        tiktok_status = "preauthorized" if self.settings.tiktok_session_path.exists() else "missing"
        return (
            "Preauthorization Setup status for VPS deployment:\n"
            f"YouTube: {youtube_status} ({self.settings.youtube_credentials_path})\n"
            f"TikTok: {tiktok_status} ({self.settings.tiktok_session_path})\n"
            "Run one-time local setup to create these files before enabling scheduled Publishing."
        )

    async def handle_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False

        args = list(getattr(context, "args", []) or [])
        with self.session_factory() as session:
            if not args:
                await update.message.reply_text(self._format_schedules(session), reply_markup=self._schedules_markup())
                return True
            if args == ["list"]:
                await update.message.reply_text(self._format_schedules(session), reply_markup=self._schedules_markup())
                return True
            if len(args) == 3 and args[0] == "add" and args[1] == "daily":
                slot = add_daily_schedule(session, self.settings, args[2])
                if slot is not None:
                    await update.message.reply_text(f"Added daily schedule {slot.id} at {slot.local_time} {slot.timezone}.")
                    return True
            if len(args) == 4 and args[0] == "add" and args[1] == "weekly":
                slot = add_weekly_schedule(session, self.settings, args[2], args[3])
                if slot is not None:
                    await update.message.reply_text(
                        f"Added weekly schedule {slot.id} on {slot.weekday} at {slot.local_time} {slot.timezone}."
                    )
                    return True
            if len(args) == 2 and args[0] == "remove" and args[1].isdigit():
                if remove_schedule(session, int(args[1])):
                    await update.message.reply_text(f"Removed schedule {args[1]}.")
                    return True

        await update.message.reply_text(self._schedule_usage())
        return False

    async def handle_schedule_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        query = getattr(update, "callback_query", None)
        if query is None:
            return False
        if await self._reject_unknown_chat(update):
            await query.answer("Unauthorized chat.")
            return False
        parts = str(getattr(query, "data", "")).split(":")
        action = parts[1] if len(parts) >= 2 and parts[0] == "schedule" else ""
        if action == "add_daily":
            await query.answer()
            await query.edit_message_text(
                "Add a daily Schedule Slot with /schedule add daily <HH:MM>.",
                reply_markup=self._schedules_markup(),
            )
            return True
        if action == "add_weekly":
            await query.answer()
            await query.edit_message_text(
                "Add a weekly Schedule Slot with /schedule add weekly <weekday> <HH:MM>.",
                reply_markup=self._schedules_markup(),
            )
            return True
        if action == "list":
            await query.answer()
            with self.session_factory() as session:
                text = self._format_schedules(session)
            await query.edit_message_text(text, reply_markup=self._schedules_markup())
            return True
        if action in {"enable", "disable", "delete"} and len(parts) == 3 and parts[2].isdigit():
            schedule_id = int(parts[2])
            with self.session_factory() as session:
                if action == "delete":
                    changed = delete_schedule(session, schedule_id)
                else:
                    changed = set_schedule_enabled(session, schedule_id, action == "enable")
            await query.answer("Schedule updated." if changed else "Unable to update Schedule Slot.")
            with self.session_factory() as session:
                text = self._format_schedules(session)
            await query.edit_message_text(text, reply_markup=self._schedules_markup())
            return changed
        await query.answer("Unknown Schedule action.")
        return False

    def _schedules_markup(self) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton("Add Daily", callback_data="schedule:add_daily")],
            [InlineKeyboardButton("Add Weekly", callback_data="schedule:add_weekly")],
            [InlineKeyboardButton("Refresh Schedules", callback_data="schedule:list")],
        ]
        with self.session_factory() as session:
            for slot in list_schedules(session):
                toggle_action = "disable" if slot.enabled else "enable"
                toggle_label = "Disable" if slot.enabled else "Enable"
                rows.append(
                    [
                        InlineKeyboardButton(f"{toggle_label} #{slot.id}", callback_data=f"schedule:{toggle_action}:{slot.id}"),
                        InlineKeyboardButton(f"Delete #{slot.id}", callback_data=f"schedule:delete:{slot.id}"),
                    ]
                )
        return InlineKeyboardMarkup(rows)

    def _format_schedules(self, session) -> str:
        schedules = list_schedules(session)
        if not schedules:
            return "No schedules configured."
        lines = ["Schedules:"]
        for slot in schedules:
            state = "enabled" if slot.enabled else "disabled"
            if slot.cadence == "weekly":
                lines.append(f"#{slot.id} weekly {slot.weekday} {slot.local_time} {slot.timezone} {state}")
            else:
                lines.append(f"#{slot.id} daily {slot.local_time} {slot.timezone} {state}")
        return "\n".join(lines)

    def _schedule_usage(self) -> str:
        return (
            "Usage: /schedule add daily <HH:MM>, "
            "/schedule add weekly <weekday> <HH:MM>, /schedule list, or /schedule remove <schedule_id>"
        )

    async def handle_clip_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        query = getattr(update, "callback_query", None)
        if query is None:
            return False
        if await self._reject_unknown_chat(update):
            await query.answer("Unauthorized chat.")
            return False

        data = str(getattr(query, "data", ""))
        parts = data.split(":")
        if len(parts) < 3 or parts[0] != "clip":
            await query.answer("Unknown action.")
            return False
        action = parts[1]
        if not parts[2].isdigit():
            await query.answer("Unknown run.")
            return False
        run_id = int(parts[2])

        if action == "toggle" and len(parts) == 4 and parts[3].isdigit():
            candidate_number = int(parts[3])
            with self.session_factory() as session:
                selected = self.manual_clipping_service.toggle_candidate_selection(session, run_id, candidate_number)
                run = session.get(RunLog, run_id)
                if selected is None or run is None:
                    await query.answer("Unable to update selection.")
                    return False
                response = self._format_highlight_review(run)
                reply_markup = self._highlight_review_markup(run)
            await query.answer(f"{'Selected' if selected else 'Unselected'} highlight {candidate_number}.")
            await query.edit_message_text(response, reply_markup=reply_markup)
            return True

        if action == "process" and len(parts) == 3:
            try:
                with self.session_factory() as session:
                    links = self.manual_clipping_service.process_selected_run(session, self.settings, run_id)
                await query.answer("Processing selected highlights.")
                if links:
                    await query.edit_message_text("Public Clip Links:\n" + "\n".join(links))
                    return True
                await query.edit_message_text("Run is queued or cannot be processed yet.")
                return False
            except Exception as exc:
                await query.answer("Processing failed.")
                await query.edit_message_text(f"Manual Clipping processing failed: {exc}")
                return False

        if action == "cancel" and len(parts) == 3:
            with self.session_factory() as session:
                cancelled = self.manual_clipping_service.cancel_run(session, run_id)
            if cancelled:
                await query.answer("Run cancelled.")
                await query.edit_message_text(f"Cancelled Run Log {run_id}.")
                return True
            await query.answer("Unable to cancel run.")
            await query.edit_message_text("Run cannot be cancelled.")
            return False

        await query.answer("Unknown action.")
        return False

    async def handle_video_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False

        message = getattr(update, "message", None)
        upload = self._extract_video_upload(message)
        if upload is None:
            if message is not None:
                await message.reply_text("Please upload a supported video file.")
            return False

        run_id = None
        try:
            with self.session_factory() as session:
                run, target_path = self.manual_clipping_service.prepare_telegram_file_run(session, upload)
                run_id = run.id
            telegram_file = await context.bot.get_file(upload.file_id)
            await telegram_file.download_to_drive(custom_path=target_path)
            with self.session_factory() as session:
                run = self.manual_clipping_service.complete_telegram_file_run(session, run_id)
                response = self._format_highlight_review(run)
                reply_markup = self._highlight_review_markup(run)
            await message.reply_text(response, reply_markup=reply_markup)
            return True
        except Exception as exc:
            if run_id is not None:
                with self.session_factory() as session:
                    self.manual_clipping_service.fail_run(session, run_id, str(exc))
            await message.reply_text(f"Manual Clipping failed: {exc}")
            return False

    def _extract_video_upload(self, message) -> TelegramVideoUpload | None:
        if message is None:
            return None
        video = getattr(message, "video", None)
        if video is not None:
            return TelegramVideoUpload(
                file_id=video.file_id,
                filename=getattr(video, "file_name", None) or f"{video.file_id}.mp4",
                content_type=getattr(video, "mime_type", None) or "video/mp4",
                file_size=getattr(video, "file_size", None),
            )
        document = getattr(message, "document", None)
        if document is not None:
            return TelegramVideoUpload(
                file_id=document.file_id,
                filename=getattr(document, "file_name", None),
                content_type=getattr(document, "mime_type", None),
                file_size=getattr(document, "file_size", None),
            )
        return None

    async def handle_clip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False

        args = list(getattr(context, "args", []) or [])
        if not args:
            await update.message.reply_text(self._clip_usage())
            return False

        if args[0] == "select" and len(args) >= 3 and args[1].isdigit():
            run_id = int(args[1])
            if not all(number.isdigit() for number in args[2:]):
                await update.message.reply_text(self._clip_usage())
                return False
            with self.session_factory() as session:
                selected = self.manual_clipping_service.select_candidates(
                    session,
                    run_id,
                    [int(number) for number in args[2:]],
                )
            if selected:
                await update.message.reply_text(f"Selected highlights for Run Log {run_id}.")
                return True
            await update.message.reply_text(self._clip_usage())
            return False

        if args[0] == "cancel" and len(args) == 2 and args[1].isdigit():
            run_id = int(args[1])
            with self.session_factory() as session:
                cancelled = self.manual_clipping_service.cancel_run(session, run_id)
            if cancelled:
                await update.message.reply_text(f"Cancelled Run Log {run_id}.")
                return True
            await update.message.reply_text(self._clip_usage())
            return False

        if args[0] == "process" and len(args) == 2 and args[1].isdigit():
            run_id = int(args[1])
            try:
                with self.session_factory() as session:
                    links = self.manual_clipping_service.process_selected_run(session, self.settings, run_id)
                if links:
                    await update.message.reply_text("Public Clip Links:\n" + "\n".join(links))
                    return True
                await update.message.reply_text("Run is queued or cannot be processed yet.")
                return False
            except Exception as exc:
                await update.message.reply_text(f"Manual Clipping processing failed: {exc}")
                return False

        if len(args) == 1:
            try:
                with self.session_factory() as session:
                    run = self.manual_clipping_service.start_run(session, args[0])
                    response = self._format_highlight_review(run)
                    reply_markup = self._highlight_review_markup(run)
                await update.message.reply_text(response, reply_markup=reply_markup)
                return True
            except Exception as exc:
                await update.message.reply_text(f"Manual Clipping failed: {exc}")
                return False

        await update.message.reply_text(self._clip_usage())
        return False

    def _highlight_review_markup(self, run) -> InlineKeyboardMarkup:
        rows = []
        for candidate in sorted(run.highlight_candidates, key=lambda item: item.candidate_number):
            state = "✅" if candidate.selected else "☐"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{state} {candidate.candidate_number}",
                        callback_data=f"clip:toggle:{run.id}:{candidate.candidate_number}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton("Process Selected", callback_data=f"clip:process:{run.id}"),
                InlineKeyboardButton("Cancel Run", callback_data=f"clip:cancel:{run.id}"),
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _format_highlight_review(self, run) -> str:
        lines = [f"Run Log {run.id} highlight candidates:"]
        for candidate in run.highlight_candidates:
            lines.extend(
                [
                    f"{candidate.candidate_number}. {candidate.title}",
                    f"Time: {candidate.start_time} - {candidate.end_time}",
                    f"Virality: {candidate.virality_score}",
                    f"Hook: {candidate.hook_text}",
                    f"Description: {candidate.description}",
                ]
            )
        lines.append(f"Select with /clip select {run.id} <numbers...> or cancel with /clip cancel {run.id}")
        return "\n".join(lines)

    def _clip_usage(self) -> str:
        return (
            "Usage: /clip <youtube_url_or_direct_video_url>, /clip select <run_id> <numbers...>, "
            "/clip process <run_id>, or /clip cancel <run_id>"
        )

    async def handle_sources(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False

        args = list(getattr(context, "args", []) or [])
        if not args:
            await update.message.reply_text(self._format_source_videos(), reply_markup=self._source_videos_markup())
            return True

        command = args[0]
        if command == "add" and len(args) >= 2:
            with self.session_factory() as session:
                sources = add_source_videos(session, args[1:])
            await update.message.reply_text(f"Added {len(sources)} Source Videos.")
            return True

        if command == "list" and len(args) == 1:
            await update.message.reply_text(self._format_source_videos(), reply_markup=self._source_videos_markup())
            return True

        if command == "remove" and len(args) == 2 and args[1].isdigit():
            source_id = int(args[1])
            with self.session_factory() as session:
                removed = cancel_pending_source_video(session, source_id)
            if removed:
                await update.message.reply_text(f"Removed Source Video {source_id}.")
                return True

        await update.message.reply_text(self._sources_usage())
        return False

    async def handle_sources_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        query = getattr(update, "callback_query", None)
        if query is None:
            return False
        if await self._reject_unknown_chat(update):
            await query.answer("Unauthorized chat.")
            return False
        parts = str(getattr(query, "data", "")).split(":")
        action = parts[1] if len(parts) >= 2 and parts[0] == "sources" else ""
        if action == "add":
            await query.answer()
            await query.edit_message_text(
                "Send source URLs with /sources add <url1> [url2 ...].",
                reply_markup=self._source_videos_markup(),
            )
            return True
        if action == "list":
            await query.answer()
            await query.edit_message_text(self._format_source_videos(), reply_markup=self._source_videos_markup())
            return True
        if action == "remove" and len(parts) == 3 and parts[2].isdigit():
            with self.session_factory() as session:
                removed = cancel_pending_source_video(session, int(parts[2]))
            await query.answer("Source Video removed." if removed else "Unable to remove Source Video.")
            await query.edit_message_text(self._format_source_videos(), reply_markup=self._source_videos_markup())
            return removed
        await query.answer("Unknown Source Video Queue action.")
        return False

    def _source_videos_markup(self) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton("Add Source", callback_data="sources:add")],
            [InlineKeyboardButton("Refresh Queue", callback_data="sources:list")],
        ]
        with self.session_factory() as session:
            for source in get_source_videos(session):
                if source.status == "pending":
                    rows.append(
                        [
                            InlineKeyboardButton(
                                f"Remove #{source.id}",
                                callback_data=f"sources:remove:{source.id}",
                            )
                        ]
                    )
        return InlineKeyboardMarkup(rows)

    def _format_source_videos(self) -> str:
        with self.session_factory() as session:
            sources = get_source_videos(session)
            if not sources:
                return "Source Video Queue is empty."
            lines = ["Source Video Queue:"]
            for source in sources:
                lines.append(f"#{source.id} [{source.status}] {source.url}")
            return "\n".join(lines)

    def _sources_usage(self) -> str:
        return "Usage: /sources add <url1> [url2 ...], /sources list, or /sources remove <source_id>"

    async def handle_defaults(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False

        args = list(getattr(context, "args", []) or [])
        if not args:
            await update.message.reply_text(self._format_workflow_defaults(), reply_markup=self._workflow_defaults_markup())
            return True

        if len(args) != 3 or args[0] != "set":
            await update.message.reply_text(self._defaults_usage())
            return False

        field, value = args[1], args[2]
        if not self._set_workflow_default(field, value):
            await update.message.reply_text(self._defaults_usage())
            return False

        await update.message.reply_text(f"Updated {self._format_default_value(field, value)}")
        return True

    async def handle_defaults_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        query = getattr(update, "callback_query", None)
        if query is None:
            return False
        if await self._reject_unknown_chat(update):
            await query.answer("Unauthorized chat.")
            return False
        parts = str(getattr(query, "data", "")).split(":")
        if len(parts) != 3 or parts[0] != "defaults" or parts[1] != "toggle":
            await query.answer("Unknown defaults action.")
            return False
        if not self._toggle_workflow_default(parts[2]):
            await query.answer("Unsupported default.")
            return False
        await query.answer("Workflow Default updated.")
        await query.edit_message_text(self._format_workflow_defaults(), reply_markup=self._workflow_defaults_markup())
        return True

    def _workflow_defaults_markup(self) -> InlineKeyboardMarkup:
        with self.session_factory() as session:
            defaults = ensure_workflow_defaults(session)
            rows = [
                [InlineKeyboardButton(f"Captions: {self._on_off(defaults.captions_enabled)}", callback_data="defaults:toggle:captions")],
                [InlineKeyboardButton(f"Hooks: {self._on_off(defaults.hooks_enabled)}", callback_data="defaults:toggle:hooks")],
                [
                    InlineKeyboardButton(
                        f"YouTube Publish: {self._on_off(defaults.publish_youtube)} ({self._publisher_auth_status('youtube')})",
                        callback_data="defaults:toggle:publish_youtube",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"TikTok Publish: {self._on_off(defaults.publish_tiktok)} ({self._publisher_auth_status('tiktok')})",
                        callback_data="defaults:toggle:publish_tiktok",
                    )
                ],
            ]
        return InlineKeyboardMarkup(rows)

    def _format_workflow_defaults(self) -> str:
        with self.session_factory() as session:
            defaults = ensure_workflow_defaults(session)
            return "\n".join(
                [
                    "Workflow Defaults:",
                    f"captions: {self._on_off(defaults.captions_enabled)}",
                    f"hooks: {self._on_off(defaults.hooks_enabled)}",
                    f"publish_youtube: {self._on_off(defaults.publish_youtube)}",
                    f"publish_tiktok: {self._on_off(defaults.publish_tiktok)}",
                    f"subtitle_language: {defaults.subtitle_language}",
                    f"YouTube preauth: {self._publisher_auth_status('youtube')}",
                    f"TikTok preauth: {self._publisher_auth_status('tiktok')}",
                ]
            )

    def _publisher_auth_status(self, platform: str) -> str:
        if platform == "youtube":
            return "preauthorized" if self.settings.youtube_credentials_path.exists() else "missing"
        if platform == "tiktok":
            return "preauthorized" if self.settings.tiktok_session_path.exists() else "missing"
        return "missing"

    def _toggle_workflow_default(self, field: str) -> bool:
        boolean_fields = {
            "captions": "captions_enabled",
            "hooks": "hooks_enabled",
            "publish_youtube": "publish_youtube",
            "publish_tiktok": "publish_tiktok",
        }
        column = boolean_fields.get(field)
        if column is None:
            return False
        with self.session_factory() as session:
            defaults = ensure_workflow_defaults(session)
            setattr(defaults, column, not getattr(defaults, column))
            session.commit()
            return True

    def _set_workflow_default(self, field: str, value: str) -> bool:
        boolean_fields = {
            "captions": "captions_enabled",
            "hooks": "hooks_enabled",
            "publish_youtube": "publish_youtube",
            "publish_tiktok": "publish_tiktok",
        }
        with self.session_factory() as session:
            defaults = ensure_workflow_defaults(session)
            if field in boolean_fields:
                if value not in {"on", "off"}:
                    return False
                setattr(defaults, boolean_fields[field], value == "on")
            elif field == "subtitle_language":
                if not value.isalpha() or not 2 <= len(value) <= 16:
                    return False
                defaults.subtitle_language = value.lower()
            else:
                return False
            session.commit()
            return True

    def _format_default_value(self, field: str, value: str) -> str:
        if field == "subtitle_language":
            return f"subtitle_language: {value.lower()}"
        return f"{field}: {value}"

    def _defaults_usage(self) -> str:
        return (
            "Usage: /defaults or /defaults set "
            "captions|hooks|publish_youtube|publish_tiktok on|off or "
            "/defaults set subtitle_language <code>"
        )

    def _on_off(self, value: bool) -> str:
        return "on" if value else "off"
