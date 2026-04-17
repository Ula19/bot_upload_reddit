"""Rate limiting — 5 запросов в минуту на юзера"""
import time
import logging
from typing import Any, Awaitable, Callable
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from bot.i18n import detect_language, t

logger = logging.getLogger(__name__)
MAX_REQUESTS = 5
WINDOW_SECONDS = 60
_user_requests: dict[int, list[float]] = {}


def cleanup_stale_entries() -> int:
    now = time.time()
    stale = [uid for uid, ts in _user_requests.items() if not any(now - t_ < WINDOW_SECONDS for t_ in ts)]
    for uid in stale:
        del _user_requests[uid]
    return len(stale)


class RateLimitMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.text:
            return await handler(event, data)
        from bot.utils.helpers import is_reddit_url
        if not is_reddit_url(event.text.strip()):
            return await handler(event, data)

        user_id = event.from_user.id
        now = time.time()
        if user_id in _user_requests:
            _user_requests[user_id] = [ts for ts in _user_requests[user_id] if now - ts < WINDOW_SECONDS]
        else:
            _user_requests[user_id] = []

        if len(_user_requests[user_id]) >= MAX_REQUESTS:
            oldest = _user_requests[user_id][0]
            wait_sec = int(WINDOW_SECONDS - (now - oldest)) + 1
            lang = detect_language(event.from_user.language_code)
            await event.answer(t("error.rate_limit", lang, seconds=wait_sec))
            return None

        _user_requests[user_id].append(now)
        return await handler(event, data)
