"""Inline-клавиатуры пользователя"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from bot.config import settings
from bot.emojis import E_ID
from bot.i18n import t


def get_start_keyboard(user_id: int, lang: str = "ru") -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=t("btn.download", lang),
            callback_data="download_reddit",
            style="primary",
            icon_custom_emoji_id=E_ID["download"],
        )],
        [
            InlineKeyboardButton(
                text=t("btn.profile", lang),
                callback_data="my_profile",
                style="success",
                icon_custom_emoji_id=E_ID["profile"],
            ),
            InlineKeyboardButton(
                text=t("btn.help", lang),
                callback_data="help",
                style="success",
                icon_custom_emoji_id=E_ID["info"],
            ),
        ],
        [InlineKeyboardButton(
            text=t("btn.language", lang),
            callback_data="change_language",
            style="success",
            icon_custom_emoji_id=E_ID["gear"],
        )],
    ]
    if user_id in settings.admin_id_list:
        buttons.append([InlineKeyboardButton(
            text=t("btn.admin_panel", lang),
            callback_data="admin_panel",
            style="danger",
            icon_custom_emoji_id=E_ID["lock"],
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_back_keyboard(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=t("btn.back", lang),
            callback_data="back_to_menu",
            style="danger",
            icon_custom_emoji_id=E_ID["back"],
        )],
    ])


def get_quality_keyboard(lang: str = "ru", qualities: dict | None = None) -> InlineKeyboardMarkup:
    if not qualities:
        qualities = {"480": 0, "720": 0}
    sorted_q = sorted(qualities.items(), key=lambda x: int(x[0]))
    rows = []
    row = []
    for quality, size_mb in sorted_q:
        label = f"{quality}p"
        if size_mb > 0:
            label += f" (~{size_mb} MB)"
        row.append(InlineKeyboardButton(
            text=label,
            callback_data=f"quality_{quality}",
            style="primary",
            icon_custom_emoji_id=E_ID["camera"],
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(
        text=t("btn.back", lang),
        callback_data="back_to_menu",
        style="danger",
        icon_custom_emoji_id=E_ID["back"],
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_subscription_keyboard(channels: list[dict], lang: str = "ru") -> InlineKeyboardMarkup:
    buttons = []
    for ch in channels:
        buttons.append([InlineKeyboardButton(
            text=ch["title"],
            url=ch["invite_link"],
            style="primary",
            icon_custom_emoji_id=E_ID["megaphone"],
        )])
    buttons.append([InlineKeyboardButton(
        text=t("btn.check_sub", lang),
        callback_data="check_subscription",
        style="success",
        icon_custom_emoji_id=E_ID["check"],
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Русский", callback_data="set_lang_ru", style="primary", icon_custom_emoji_id=E_ID["flag_ru"]),
        InlineKeyboardButton(text="O'zbek", callback_data="set_lang_uz", style="primary", icon_custom_emoji_id=E_ID["flag_uz"]),
        InlineKeyboardButton(text="English", callback_data="set_lang_en", style="primary", icon_custom_emoji_id=E_ID["flag_gb"]),
    ]])
