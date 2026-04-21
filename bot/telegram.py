"""
claw8s.bot.telegram
---------------------
Telegram bot serving two purposes:
  1. PUSH: sends alerts and agent summaries to you when incidents happen
  2. PULL: lets you query status, view history, and approve/reject pending actions

Commands:
  /start    - welcome + confirm you're authorized
  /status   - current cluster summary (nodes, unhealthy pods)
  /history  - last 10 incidents from audit log
  /approve  - approve a pending action (used in reply to approval request)
  /reject   - reject a pending action
  /help     - list commands

Approval flow:
  - When the agent wants to do something destructive below its confidence threshold,
    it sends you a Telegram message with Approve/Reject inline buttons.
  - Your response is fed back to the agent via an asyncio.Event.
"""

import asyncio
import logging
from typing import Optional, Callable

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from config import TelegramConfig
from audit import AuditLog

log = logging.getLogger(__name__)


class TelegramBot:
    def __init__(
        self,
        cfg: TelegramConfig,
        token: str,
        audit: AuditLog,
        cluster_status_fn: Optional[Callable] = None,  # async fn → str
    ):
        self.cfg = cfg
        self.audit = audit
        self.cluster_status_fn = cluster_status_fn

        self._app = Application.builder().token(token).build()
        self._pending: dict[str, asyncio.Future] = {}  # callback_id → Future[bool]

        # Register handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("history", self._cmd_history))
        self._app.add_handler(CallbackQueryHandler(self._handle_approval))

        # Primary chat ID — we'll learn it on first /start, or use from config
        self._primary_chat_id: Optional[int] = self.cfg.primary_chat_id

    # ─── Public API ───────────────────────────────────────────────

    async def start(self):
        """Start polling in the background."""
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot started")

    async def stop(self):
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    async def send_alert(self, text: str):
        """Push a plain alert message to the primary chat."""
        if not self._primary_chat_id:
            log.warning("No primary chat ID set — cannot send alert")
            return
        await self._app.bot.send_message(
            chat_id=self._primary_chat_id,
            text=text,
            parse_mode="Markdown",
        )

    async def request_approval(
        self,
        incident_id: str,
        tool_name: str,
        tool_args: dict,
        reasoning: str,
        confidence: float,
    ) -> bool:
        """
        Send an approval request to Telegram with Approve/Reject buttons.
        Blocks until the user responds (or times out after 5 minutes).
        """
        if not self._primary_chat_id:
            log.warning("No primary chat ID — auto-rejecting approval request")
            return False

        callback_id = f"{incident_id}:{tool_name}"
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[callback_id] = future

        args_str = "\n".join(f"  `{k}`: `{v}`" for k, v in tool_args.items())
        text = (
            f"⚠️ *Approval Required*\n\n"
            f"*Action:* `{tool_name}`\n"
            f"*Incident:* `{incident_id[:8]}...`\n"
            f"*Confidence:* {confidence:.0%}\n"
            f"*Args:*\n{args_str}\n\n"
            f"*Reasoning:* {reasoning[:400]}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{callback_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{callback_id}"),
            ]
        ])

        await self._app.bot.send_message(
            chat_id=self._primary_chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

        try:
            result = await asyncio.wait_for(future, timeout=300)  # 5-minute timeout
            return result
        except asyncio.TimeoutError:
            log.warning(f"Approval timed out for {callback_id} — auto-rejecting")
            self._pending.pop(callback_id, None)
            return False

    # ─── Telegram command handlers ────────────────────────────────

    def _is_authorized(self, user_id: int) -> bool:
        if not self.cfg.allowed_user_ids:
            return True  # open to anyone if not configured
        return user_id in self.cfg.allowed_user_ids

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not self._is_authorized(user.id):
            await update.message.reply_text("⛔ Not authorized.")
            return
        self._primary_chat_id = update.effective_chat.id
        await update.message.reply_text(
            f"🦅 *Claw8s online!*\n\n"
            f"Hi {user.first_name}. I'm monitoring your cluster.\n"
            f"Chat ID `{self._primary_chat_id}` registered as primary alert target.\n\n"
            f"Type /help for available commands.",
            parse_mode="Markdown",
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        await update.message.reply_text(
            "*Claw8s Commands*\n\n"
            "/status — cluster health overview\n"
            "/history — last 10 incidents\n"
            "/start — register this chat for alerts\n"
            "/help — this message",
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        if self.cluster_status_fn:
            status = await self.cluster_status_fn()
        else:
            status = "No status function configured."
        await update.message.reply_text(f"📊 *Cluster Status*\n\n{status}", parse_mode="Markdown")

    async def _cmd_history(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update.effective_user.id):
            return
        incidents = await self.audit.get_recent_incidents(limit=10)
        if not incidents:
            await update.message.reply_text("No incidents recorded yet.")
            return
        lines = ["📋 *Recent Incidents*\n"]
        for i in incidents:
            lines.append(
                f"• `{i['incident_id'][:8]}` | {i['timestamp'][:16]} | "
                f"{i['object_kind']}/{i['object_name']} | *{i['reason']}*"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _handle_approval(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if not self._is_authorized(query.from_user.id):
            await query.edit_message_text("⛔ Not authorized.")
            return

        data = query.data  # "approve:incident_id:tool_name" or "reject:..."
        parts = data.split(":", 1)
        if len(parts) != 2:
            return

        action, callback_id = parts
        future = self._pending.pop(callback_id, None)

        if future is None:
            await query.edit_message_text("⏰ This approval request has expired.")
            return

        approved = action == "approve"
        future.set_result(approved)

        emoji = "✅" if approved else "❌"
        await query.edit_message_text(
            f"{emoji} {'Approved' if approved else 'Rejected'} by {query.from_user.first_name}",
            parse_mode="Markdown",
        )
