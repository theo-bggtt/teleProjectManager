"""User ID whitelist decorator."""
import logging
from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def restricted(allowed_ids: set[int]):
    def decorator(handler):
        @wraps(handler)
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user = update.effective_user
            user_id = user.id if user else None
            if user_id not in allowed_ids:
                logger.warning("Unauthorized access by user_id=%s username=%s",
                               user_id, getattr(user, "username", None))
                if update.effective_message:
                    await update.effective_message.reply_text("Unauthorized.")
                return
            return await handler(update, context, *args, **kwargs)
        return wrapped
    return decorator
