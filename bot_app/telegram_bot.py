from typing import Protocol

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

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
        await update.message.reply_text("Available commands: /start, /help, /status")
        return True

    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if await self._reject_unknown_chat(update):
            return False
        await update.message.reply_text("Bot Control Mode status: ok")
        return True
