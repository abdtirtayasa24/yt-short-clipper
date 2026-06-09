from typing import Protocol

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from bot_app.database import create_session_factory, ensure_workflow_defaults
from bot_app.settings import Settings


class TelegramBotShell(Protocol):
    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...


class AuthorizedOperatorTelegramBot:
    """Command-first Telegram shell restricted to the Authorized Operator."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.session_factory = create_session_factory(settings.database_url)
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
        self.started = True

    async def stop(self) -> None:
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
        )
        return True

    async def handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False
        await update.message.reply_text("Available commands: /start, /help, /status, /defaults")
        return True

    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False
        await update.message.reply_text("Bot Control Mode status: ok")
        return True

    async def handle_defaults(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False

        args = list(getattr(context, "args", []) or [])
        if not args:
            await update.message.reply_text(self._format_workflow_defaults())
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
                ]
            )

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
