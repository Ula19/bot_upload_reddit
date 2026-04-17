"""Хэндлер /start"""
import logging
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from bot.config import settings
from bot.database import async_session
from bot.database.crud import get_or_create_user, get_user_language, update_user_language
from bot.emojis import E
from bot.i18n import detect_language, t
from bot.keyboards.inline import get_back_keyboard, get_language_keyboard, get_start_keyboard

logger = logging.getLogger(__name__)
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    detected_lang = detect_language(message.from_user.language_code)
    async with async_session() as session:
        await get_or_create_user(
            session=session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
            language=detected_lang,
        )
        lang = await get_user_language(session, message.from_user.id)
    await message.answer(
        t("start.welcome", lang, name=message.from_user.first_name),
        reply_markup=get_start_keyboard(user_id=message.from_user.id, lang=lang),
        parse_mode="HTML",
    )
    from bot.utils.commands import set_user_commands
    await set_user_commands(message.bot, message.from_user.id, lang)


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with async_session() as session:
        lang = await get_user_language(session, callback.from_user.id)
    await callback.message.edit_text(
        t("start.welcome", lang, name=callback.from_user.first_name),
        reply_markup=get_start_keyboard(user_id=callback.from_user.id, lang=lang),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_panel")
async def open_admin_panel(callback: CallbackQuery) -> None:
    from bot.keyboards.admin import get_admin_keyboard
    if callback.from_user.id not in settings.admin_id_list:
        await callback.answer(f"{E['ban']} Нет доступа")
        return
    async with async_session() as session:
        lang = await get_user_language(session, callback.from_user.id)
    await callback.message.edit_text(t("admin.title", lang), reply_markup=get_admin_keyboard(lang))
    await callback.answer()


@router.callback_query(F.data == "download_reddit")
async def download_reddit_prompt(callback: CallbackQuery) -> None:
    async with async_session() as session:
        lang = await get_user_language(session, callback.from_user.id)
    await callback.message.edit_text(
        t("download.prompt", lang),
        reply_markup=get_back_keyboard(lang),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "my_profile")
async def my_profile(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user = await get_or_create_user(
            session=session,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name,
        )
        lang = user.language or "ru"
    await callback.message.edit_text(
        t("profile.title", lang, full_name=callback.from_user.full_name, user_id=callback.from_user.id, downloads=user.download_count),
        reply_markup=get_back_keyboard(lang),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "help")
async def help_handler(callback: CallbackQuery) -> None:
    async with async_session() as session:
        lang = await get_user_language(session, callback.from_user.id)
    await callback.message.edit_text(
        t("help.text", lang, admin_username=settings.admin_username),
        reply_markup=get_back_keyboard(lang),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "change_language")
async def change_language(callback: CallbackQuery) -> None:
    async with async_session() as session:
        lang = await get_user_language(session, callback.from_user.id)
    await callback.message.edit_text(t("lang.choose", lang), reply_markup=get_language_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("set_lang_"))
async def set_language(callback: CallbackQuery) -> None:
    lang = callback.data.replace("set_lang_", "")
    if lang not in ("ru", "uz", "en"):
        return
    async with async_session() as session:
        await update_user_language(session, callback.from_user.id, lang)
    await callback.message.edit_text(
        t("lang.changed", lang),
        reply_markup=get_start_keyboard(user_id=callback.from_user.id, lang=lang),
        parse_mode="HTML",
    )
    await callback.answer()
    from bot.utils.commands import set_user_commands
    await set_user_commands(callback.bot, callback.from_user.id, lang)


@router.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery, state: FSMContext) -> None:
    from bot.database.crud import get_active_channels
    from bot.keyboards.inline import get_subscription_keyboard
    from bot.middlewares.subscription import is_subscribed
    async with async_session() as session:
        channels = await get_active_channels(session)
        lang = await get_user_language(session, callback.from_user.id)

    if not channels:
        await callback.answer(t("sub.not_required", lang))
        return

    bot = callback.bot
    not_subscribed = []
    for channel in channels:
        if not await is_subscribed(bot, channel.channel_id, callback.from_user.id):
            not_subscribed.append({"title": channel.title, "invite_link": channel.invite_link})

    if not_subscribed:
        await callback.message.edit_text(
            t("sub.not_subscribed", lang),
            reply_markup=get_subscription_keyboard(not_subscribed, lang),
            parse_mode="HTML",
        )
        await callback.answer(t("sub.check_alert_fail", lang), show_alert=True)
    else:
        fsm_data = await state.get_data()
        pending_url = fsm_data.get("pending_url")
        await state.clear()
        await callback.message.edit_text(
            t("sub.success", lang, name=callback.from_user.first_name),
            reply_markup=get_start_keyboard(user_id=callback.from_user.id, lang=lang),
            parse_mode="HTML",
        )
        await callback.answer(t("sub.check_alert_ok", lang))
        if pending_url:
            try:
                from bot.handlers.download import _process_reddit_link
                await _process_reddit_link(callback.message, pending_url, callback.from_user, lang)
            except Exception as e:
                logger.error("Ошибка обработки pending_url: %s", e)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with async_session() as session:
        lang = await get_user_language(session, message.from_user.id)
    await message.answer(
        t("start.welcome", lang, name=message.from_user.first_name),
        reply_markup=get_start_keyboard(user_id=message.from_user.id, lang=lang),
        parse_mode="HTML",
    )


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    async with async_session() as session:
        user = await get_or_create_user(
            session=session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )
        lang = user.language or "ru"
    await message.answer(
        t("profile.title", lang, full_name=message.from_user.full_name, user_id=message.from_user.id, downloads=user.download_count),
        reply_markup=get_back_keyboard(lang),
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    async with async_session() as session:
        lang = await get_user_language(session, message.from_user.id)
    await message.answer(
        t("help.text", lang, admin_username=settings.admin_username),
        reply_markup=get_back_keyboard(lang),
        parse_mode="HTML",
    )


@router.message(Command("language"))
async def cmd_language(message: Message) -> None:
    async with async_session() as session:
        lang = await get_user_language(session, message.from_user.id)
    await message.answer(t("lang.choose", lang), reply_markup=get_language_keyboard(), parse_mode="HTML")
