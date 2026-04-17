"""Админ-панель"""
import logging
import asyncio
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from bot.config import settings
from bot.database import async_session
from bot.database.crud import (
    add_channel, get_active_channels, get_all_user_ids,
    get_user_language, get_user_stats, remove_channel,
)
from bot.emojis import E, E_ID
from bot.i18n import t
from bot.keyboards.admin import get_admin_keyboard, get_cancel_keyboard, get_channels_keyboard

logger = logging.getLogger(__name__)
router = Router()


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_id_list


async def _get_lang(user_id: int) -> str:
    async with async_session() as session:
        return await get_user_language(session, user_id)


class AddChannelStates(StatesGroup):
    waiting_channel_id = State()
    waiting_title = State()
    waiting_invite_link = State()


class BroadcastStates(StatesGroup):
    waiting_message = State()
    confirming = State()


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        lang = await _get_lang(message.from_user.id)
        await message.answer(t("admin.no_access", lang))
        return
    lang = await _get_lang(message.from_user.id)
    await message.answer(t("admin.title", lang), reply_markup=get_admin_keyboard(lang))


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(f"{E['lock']} Нет доступа")
        return
    lang = await _get_lang(callback.from_user.id)
    async with async_session() as session:
        stats = await get_user_stats(session)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("btn.admin_back", lang), callback_data="admin_panel", style="danger", icon_custom_emoji_id=E_ID["back"])
    ]])
    await callback.message.edit_text(t("admin.stats", lang, **stats), reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_channels")
async def admin_channels(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(f"{E['lock']} Нет доступа")
        return
    lang = await _get_lang(callback.from_user.id)
    async with async_session() as session:
        channels = await get_active_channels(session)
    if channels:
        text = t("admin.channels_title", lang)
        for ch in channels:
            text += f"\n• {ch.title} (<code>{ch.channel_id}</code>)"
    else:
        text = t("admin.channels_empty", lang)
    await callback.message.edit_text(text, reply_markup=get_channels_keyboard(channels, lang), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_add_channel")
async def admin_add_channel(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(f"{E['lock']} Нет доступа")
        return
    lang = await _get_lang(callback.from_user.id)
    await state.set_state(AddChannelStates.waiting_channel_id)
    await callback.message.edit_text(t("admin.add_channel_id", lang), reply_markup=get_cancel_keyboard(lang), parse_mode="HTML")
    await callback.answer()


@router.message(AddChannelStates.waiting_channel_id)
async def process_channel_id(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(message.from_user.id)
    try:
        channel_id = int(message.text.strip())
    except ValueError:
        await message.answer(t("admin.id_not_number", lang), reply_markup=get_cancel_keyboard(lang))
        return
    await state.update_data(channel_id=channel_id)
    await state.set_state(AddChannelStates.waiting_title)
    await message.answer(t("admin.add_channel_title", lang), reply_markup=get_cancel_keyboard(lang), parse_mode="HTML")


@router.message(AddChannelStates.waiting_title)
async def process_channel_title(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(message.from_user.id)
    title = message.text.strip()
    if len(title) > 200:
        await message.answer(t("admin.title_too_long", lang), reply_markup=get_cancel_keyboard(lang))
        return
    await state.update_data(title=title)
    await state.set_state(AddChannelStates.waiting_invite_link)
    await message.answer(t("admin.add_channel_link", lang), reply_markup=get_cancel_keyboard(lang), parse_mode="HTML")


def _normalize_channel_link(raw: str) -> str | None:
    import re
    raw = raw.strip()
    patterns = [
        (r"https?://(t\.me|telegram\.me)/(\S+)", r"https://t.me/\2"),
        (r"@(\S+)", r"https://t.me/\1"),
        (r"^([a-zA-Z0-9_]+)$", r"https://t.me/\1"),
    ]
    for pattern, replacement in patterns:
        m = re.match(pattern, raw)
        if m:
            return re.sub(pattern, replacement, raw)
    return None


@router.message(AddChannelStates.waiting_invite_link)
async def process_channel_link(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(message.from_user.id)
    link = _normalize_channel_link(message.text)
    if not link:
        await message.answer(t("admin.link_invalid", lang), reply_markup=get_cancel_keyboard(lang))
        return
    data = await state.get_data()
    await state.clear()
    async with async_session() as session:
        try:
            await add_channel(session, data["channel_id"], data["title"], link)
        except ValueError as e:
            await message.answer(f"{E['cross']} {e}")
            return
    await message.answer(t("admin.channel_added", lang), parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_del_"))
async def admin_del_channel(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(f"{E['lock']} Нет доступа")
        return
    lang = await _get_lang(callback.from_user.id)
    channel_id = int(callback.data.replace("admin_del_", ""))
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("btn.admin_confirm_del", lang), callback_data=f"admin_confirm_del_{channel_id}", style="danger", icon_custom_emoji_id=E_ID["trash"])],
        [InlineKeyboardButton(text=t("btn.admin_cancel_del", lang), callback_data="admin_channels", style="success", icon_custom_emoji_id=E_ID["back"])],
    ])
    await callback.message.edit_text(t("admin.confirm_delete", lang, channel_id=channel_id), reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_confirm_del_"))
async def admin_confirm_del(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(f"{E['lock']} Нет доступа")
        return
    channel_id = int(callback.data.replace("admin_confirm_del_", ""))
    async with async_session() as session:
        await remove_channel(session, channel_id)
    lang = await _get_lang(callback.from_user.id)
    async with async_session() as session:
        channels = await get_active_channels(session)
    await callback.message.edit_text(
        t("admin.channels_title", lang) if channels else t("admin.channels_empty", lang),
        reply_markup=get_channels_keyboard(channels, lang),
        parse_mode="HTML",
    )
    await callback.answer(f"{E['check']} Удалено")


@router.callback_query(F.data == "admin_cancel")
async def admin_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    lang = await _get_lang(callback.from_user.id)
    await callback.message.edit_text(t("admin.title", lang), reply_markup=get_admin_keyboard(lang))
    await callback.answer()


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(f"{E['lock']} Нет доступа")
        return
    lang = await _get_lang(callback.from_user.id)
    await state.set_state(BroadcastStates.waiting_message)
    await callback.message.edit_text(t("admin.broadcast_prompt", lang), reply_markup=get_cancel_keyboard(lang), parse_mode="HTML")
    await callback.answer()


@router.message(BroadcastStates.waiting_message)
async def process_broadcast_message(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(message.from_user.id)
    await state.update_data(
        text=message.text,
        photo=message.photo[-1].file_id if message.photo else None,
        video=message.video.file_id if message.video else None,
        caption=message.caption,
    )
    await state.set_state(BroadcastStates.confirming)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("admin.broadcast_confirm", lang), callback_data="admin_broadcast_confirm", style="success", icon_custom_emoji_id=E_ID["check"])],
        [InlineKeyboardButton(text=t("admin.broadcast_cancel", lang), callback_data="admin_cancel", style="danger", icon_custom_emoji_id=E_ID["cross"])],
    ])
    await message.answer(t("admin.broadcast_preview", lang), reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_broadcast_confirm")
async def confirm_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer(f"{E['lock']} Нет доступа")
        return
    lang = await _get_lang(callback.from_user.id)
    data = await state.get_data()
    await state.clear()
    await callback.message.edit_text(t("admin.broadcast_started", lang))
    await callback.answer()

    async with async_session() as session:
        user_ids = await get_all_user_ids(session)

    skip_ids = set(settings.admin_id_list) | {callback.from_user.id}
    success = failed = 0
    for i, uid in enumerate(user_ids):
        if uid in skip_ids:
            continue
        try:
            if data.get("photo"):
                await callback.bot.send_photo(uid, data["photo"], caption=data.get("caption"), parse_mode="HTML")
            elif data.get("video"):
                await callback.bot.send_video(uid, data["video"], caption=data.get("caption"), parse_mode="HTML")
            elif data.get("text"):
                await callback.bot.send_message(uid, data["text"], parse_mode="HTML")
            success += 1
        except Exception:
            failed += 1
        if i % 25 == 0:
            await asyncio.sleep(1)

    total = success + failed
    await callback.bot.send_message(
        callback.from_user.id,
        t("admin.broadcast_done", lang, success=success, failed=failed, total=total),
        parse_mode="HTML",
    )
