"""Мидлварь проверки подписки"""
import logging
from typing import Any, Awaitable, Callable
from aiogram import BaseMiddleware, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject
from bot.config import settings
from bot.database import async_session
from bot.database.crud import get_active_channels, get_user_language
from bot.i18n import t
from bot.keyboards.inline import get_subscription_keyboard

logger = logging.getLogger(__name__)
SKIP_CALLBACKS = {"check_subscription", "set_lang_ru", "set_lang_uz", "set_lang_en", "change_language"}


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery) and (
            event.data in SKIP_CALLBACKS or event.data.startswith("admin")
        ):
            return await handler(event, data)

        user = None
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user

        if user and user.id in settings.admin_id_list:
            return await handler(event, data)

        async with async_session() as session:
            channels = await get_active_channels(session)

        if not channels:
            return await handler(event, data)

        bot: Bot = data["bot"]
        not_subscribed = []
        for channel in channels:
            if not await is_subscribed(bot, channel.channel_id, user.id):
                not_subscribed.append({"title": channel.title, "invite_link": channel.invite_link})

        if not not_subscribed:
            return await handler(event, data)

        from bot.i18n import detect_language
        lang = detect_language(user.language_code) if user else "uz"
        text = t("sub.welcome", lang)
        keyboard = get_subscription_keyboard(not_subscribed, lang)

        if isinstance(event, Message) and event.text:
            from bot.utils.helpers import is_reddit_url
            if is_reddit_url(event.text.strip()):
                state: FSMContext | None = data.get("state")
                if state:
                    await state.update_data(pending_url=event.text.strip())

        if isinstance(event, Message):
            await event.answer(text, reply_markup=keyboard, parse_mode="HTML")
        elif isinstance(event, CallbackQuery):
            await event.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            await event.answer()
        return None


async def is_subscribed(bot: Bot, channel_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(channel_id, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning("Не удалось проверить подписку %s на %s: %s", user_id, channel_id, e)
        return False
