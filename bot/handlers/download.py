"""Хэндлер скачивания — обрабатывает ссылки Reddit
Флоу: ссылка → определение типа → [выбор качества для видео] → скачивание → отправка
Галереи: SendMediaGroup по 10 элементов
"""
import asyncio
import logging
import os
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from bot.database import async_session
from bot.database.crud import (
    get_cached_download,
    get_or_create_user,
    get_user_language,
    save_download,
)
from bot.i18n import t
from bot.keyboards.inline import get_back_keyboard, get_quality_keyboard
from bot.services.reddit import FileTooLargeError, classify_error, downloader
from bot.utils.helpers import clean_reddit_url, is_reddit_url
from bot.utils.video_meta import get_video_meta
from bot.config import settings
from bot.emojis import E

logger = logging.getLogger(__name__)
router = Router()

PROGRESS_UPDATE_INTERVAL = 4
_FALLBACK_ALERT_THROTTLE = 600
_last_fallback_alert: dict[str, float] = {}

_ERROR_CATEGORY_LABELS = {
    "auth_required": "Reddit требует авторизацию (NSFW/приват)",
    "ip_blocked": "Reddit заблокировал IP — нужна ротация прокси",
    "network": "Сетевая ошибка (таймаут/нет связи)",
    "unknown": "Неизвестная ошибка",
}

_SILENT_CATEGORIES = {"unavailable"}
_download_semaphore = asyncio.Semaphore(30)


class DownloadStates(StatesGroup):
    waiting_quality = State()


@router.message(F.text)
async def handle_reddit_link(message: Message, state: FSMContext) -> None:
    """Обработка текстовых сообщений — ищем ссылки Reddit"""
    text = message.text.strip()

    async with async_session() as session:
        lang = await get_user_language(session, message.from_user.id)

    if not is_reddit_url(text):
        await message.answer(t("download.not_reddit", lang), parse_mode="HTML")
        return

    clean_url = clean_reddit_url(text)
    await _process_reddit_link(message, clean_url, message.from_user, lang, state)


async def _process_reddit_link(
    message: Message, url: str, user, lang: str, state: FSMContext | None = None
) -> None:
    """Обрабатывает Reddit ссылку: получает инфо, определяет тип, скачивает"""
    status_msg = await message.answer(t("download.fetching_info", lang))

    try:
        info = await downloader.get_info(url)
    except Exception as e:
        logger.error("Ошибка получения инфо: %s", e)
        error_text = _get_error_text(str(e), lang)
        await status_msg.edit_text(error_text, parse_mode="HTML")
        return

    if info is None:
        await status_msg.edit_text(t("download.no_media", lang), parse_mode="HTML")
        return

    # === Видео — показываем выбор качества ===
    if info.media_type == "video":
        qualities = info.qualities or {"360": 0, "720": 0}
        # фильтруем слишком большие
        max_mb = settings.max_quality_size_mb
        filtered = {q: s for q, s in qualities.items() if s == 0 or s <= max_mb}
        if not filtered:
            await status_msg.edit_text(t("error.too_large", lang), parse_mode="HTML")
            return

        if state:
            await state.set_state(DownloadStates.waiting_quality)
            await state.update_data(
                url=url,
                qualities=filtered,
                media_type="video",
                title=info.title,
                dash_video_url=info.dash_video_url,
                dash_audio_urls=info.dash_audio_urls,
            )

        await status_msg.edit_text(
            t("download.choose_quality", lang),
            reply_markup=get_quality_keyboard(lang, filtered),
            parse_mode="HTML",
        )
        return

    # === Фото / GIF — скачиваем сразу ===
    if info.media_type in ("photo", "gif"):
        await _process_photo(message, url, info, user, lang, status_msg)
        return

    # === Галерея — скачиваем все и отправляем SendMediaGroup ===
    if info.media_type == "gallery":
        await _process_gallery(message, url, info, user, lang, status_msg)
        return

    await status_msg.edit_text(t("download.no_media", lang), parse_mode="HTML")


@router.callback_query(F.data.startswith("quality_"))
async def choose_quality(callback: CallbackQuery, state: FSMContext) -> None:
    """Юзер выбрал качество видео"""
    quality = callback.data.replace("quality_", "")
    data = await state.get_data()
    url = data.get("url")
    qualities = data.get("qualities") or {}
    await state.clear()

    await callback.answer()

    if not url:
        await callback.message.answer(f"{E['cross']} Ссылка не найдена, отправь заново")
        return

    async with async_session() as session:
        lang = await get_user_language(session, callback.from_user.id)

    format_key = f"video_{quality}"

    # проверяем кэш
    async with async_session() as session:
        cached = await get_cached_download(session, url, format_key)
    if cached:
        logger.info("Кэш найден для %s [%s]", url, format_key)
        await _send_cached(callback.message, cached.file_id, cached.media_type)
        return

    async with _download_semaphore:
        status_msg = await callback.message.edit_text(t("download.processing", lang))

        from bot.services.reddit import MediaInfo
        info = MediaInfo(
            title=data.get("title", "Reddit Video"),
            media_type="video",
            qualities=qualities,
            dash_video_url=data.get("dash_video_url"),
            dash_audio_urls=data.get("dash_audio_urls") or [],
        )

        result = None
        try:
            result = await downloader.download_video(url, quality, info=info)
            file_id = await _send_video(callback.message, result, status_msg, lang)

            if file_id:
                async with async_session() as session:
                    await save_download(session, url, result.format_key or format_key, file_id, "video")
                    user_obj = await get_or_create_user(
                        session=session,
                        telegram_id=callback.from_user.id,
                        username=callback.from_user.username,
                        full_name=callback.from_user.full_name,
                    )
                    user_obj.download_count += 1
                    await session.commit()

            try:
                await status_msg.delete()
            except Exception:
                pass

        except FileTooLargeError:
            current_q = int(quality)
            lower = {q: s for q, s in qualities.items() if int(q) < current_q}
            if lower:
                await status_msg.edit_text(
                    t("error.too_large_try_lower", lang),
                    reply_markup=get_quality_keyboard(lang, lower),
                    parse_mode="HTML",
                )
                await state.set_state(DownloadStates.waiting_quality)
                await state.update_data(url=url, qualities=lower, **{
                    k: data[k] for k in ("title", "dash_video_url", "dash_audio_urls") if k in data
                })
            else:
                await status_msg.edit_text(t("error.too_large", lang), parse_mode="HTML")

        except Exception as e:
            logger.error("Ошибка скачивания %s: %s", url, e)
            await status_msg.edit_text(_get_error_text(str(e), lang), parse_mode="HTML")

        finally:
            if result:
                downloader.cleanup(result)


async def _process_photo(
    message: Message, url: str, info, user, lang: str, status_msg: Message
) -> None:
    """Скачивает и отправляет фото/GIF"""
    format_key = info.media_type  # photo или gif

    # проверяем кэш
    async with async_session() as session:
        cached = await get_cached_download(session, url, format_key)
    if cached:
        await _send_cached(message, cached.file_id, cached.media_type)
        try:
            await status_msg.delete()
        except Exception:
            pass
        return

    async with _download_semaphore:
        await status_msg.edit_text(t("download.processing", lang))
        result = None
        try:
            result = await downloader.download_photo(url, info)

            try:
                await status_msg.edit_text(t("download.uploading", lang))
            except Exception:
                pass

            file_id = await _send_photo_or_gif(message, result, lang)

            if file_id:
                async with async_session() as session:
                    await save_download(session, url, format_key, file_id, result.media_type)
                    user_obj = await get_or_create_user(
                        session=session,
                        telegram_id=user.id,
                        username=user.username,
                        full_name=user.full_name,
                    )
                    user_obj.download_count += 1
                    await session.commit()

            try:
                await status_msg.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error("Ошибка скачивания фото %s: %s", url, e)
            await status_msg.edit_text(_get_error_text(str(e), lang), parse_mode="HTML")

        finally:
            if result:
                downloader.cleanup(result)


async def _process_gallery(
    message: Message, url: str, info, user, lang: str, status_msg: Message
) -> None:
    """Скачивает галерею и отправляет SendMediaGroup по 10 элементов"""
    async with _download_semaphore:
        await status_msg.edit_text(t("download.processing", lang))
        results = []
        try:
            results = await downloader.download_gallery(info)

            if not results:
                await status_msg.edit_text(t("download.no_media", lang), parse_mode="HTML")
                return

            # отправляем пачками по 10 (лимит Telegram SendMediaGroup)
            total_batches = (len(results) + 9) // 10
            last_batch_idx = total_batches - 1
            for batch_idx in range(total_batches):
                batch = results[batch_idx * 10:(batch_idx + 1) * 10]

                try:
                    await status_msg.edit_text(
                        t("download.gallery_uploading", lang,
                          current=batch_idx + 1, total=total_batches)
                    )
                except Exception:
                    pass

                media_group = []
                for i, r in enumerate(batch):
                    file = FSInputFile(r.file_path)
                    if r.media_type == "gif" or r.file_path.endswith(".mp4"):
                        meta = await get_video_meta(r.file_path)
                        media = InputMediaVideo(
                            media=file,
                            width=meta.get("width"),
                            height=meta.get("height"),
                            duration=meta.get("duration"),
                        )
                    else:
                        media = InputMediaPhoto(media=file)

                    # caption на первом элементе последней пачки
                    if batch_idx == last_batch_idx and i == 0:
                        promo = t("download.promo", lang, bot_username=settings.bot_username)
                        media.caption = f"{E['folder']} {info.title}{promo}"

                    media_group.append(media)

                try:
                    await message.answer_media_group(media=media_group)
                except TelegramRetryAfter as e:
                    logger.warning("Telegram rate limit (gallery), ждём %ds", e.retry_after)
                    await asyncio.sleep(e.retry_after)
                    await message.answer_media_group(media=media_group)

            # обновляем счётчик
            async with async_session() as session:
                user_obj = await get_or_create_user(
                    session=session,
                    telegram_id=user.id,
                    username=user.username,
                    full_name=user.full_name,
                )
                user_obj.download_count += 1
                await session.commit()

            try:
                await status_msg.delete()
            except Exception:
                pass

        except Exception as e:
            logger.error("Ошибка скачивания галереи %s: %s", url, e)
            await status_msg.edit_text(_get_error_text(str(e), lang), parse_mode="HTML")

        finally:
            if results:
                downloader.cleanup_many(results)


async def _send_video(message: Message, result, status_msg=None, lang="ru") -> str | None:
    """Отправляет видео юзеру и возвращает file_id"""
    file = FSInputFile(result.file_path)

    if status_msg:
        try:
            await status_msg.edit_text(t("download.uploading", lang))
        except Exception:
            pass

    # ffprobe — реальные метаданные файла (width/height/duration)
    meta = await get_video_meta(result.file_path)
    width = meta.get("width") or result.width
    height = meta.get("height") or result.height
    duration = meta.get("duration") or (int(result.duration) if result.duration else None)

    t_upload = time.monotonic()
    try:
        size_mb = os.path.getsize(result.file_path) / 1024 / 1024
    except OSError:
        size_mb = 0

    promo = t("download.promo", lang, bot_username=settings.bot_username)
    try:
        sent = await message.answer_video(
            video=file,
            caption=f"{E['video']} {result.title}{promo}",
            duration=duration,
            width=width,
            height=height,
        )
    except TelegramRetryAfter as e:
        logger.warning("Telegram rate limit, ждём %ds", e.retry_after)
        await asyncio.sleep(e.retry_after)
        file = FSInputFile(result.file_path)
        sent = await message.answer_video(
            video=file,
            caption=f"{E['video']} {result.title}{promo}",
            duration=duration,
            width=width,
            height=height,
        )

    elapsed = time.monotonic() - t_upload
    speed = size_mb / elapsed if elapsed > 0 else 0
    logger.info("[METRIC] upload_video %.2fs size=%.1fMB speed=%.1fMB/s", elapsed, size_mb, speed)

    return sent.video.file_id


async def _send_photo_or_gif(message: Message, result, lang="ru") -> str | None:
    """Отправляет фото или GIF.
    Длинные mp4 (> 10 сек) отправляем как видео — Telegram не считает их анимацией.
    """
    file = FSInputFile(result.file_path)
    promo = t("download.promo", lang, bot_username=settings.bot_username)

    is_mp4 = result.media_type == "gif" or result.file_path.endswith(".mp4")

    if is_mp4:
        meta = await get_video_meta(result.file_path)
        duration = meta.get("duration") or 0

        # длинные mp4 (> 10 сек) — это видео, не анимация
        if duration > 10:
            try:
                sent = await message.answer_video(
                    video=file,
                    caption=f"{E['video']} {result.title}{promo}",
                    width=meta.get("width"),
                    height=meta.get("height"),
                    duration=duration,
                )
            except TelegramRetryAfter as e:
                logger.warning("Telegram rate limit, ждём %ds", e.retry_after)
                await asyncio.sleep(e.retry_after)
                file = FSInputFile(result.file_path)
                sent = await message.answer_video(
                    video=file, caption=f"{E['video']} {result.title}{promo}",
                    width=meta.get("width"), height=meta.get("height"), duration=duration,
                )
            return sent.video.file_id

        # короткие mp4 — анимация (GIF)
        try:
            sent = await message.answer_animation(
                animation=file,
                caption=f"{E['video']} {result.title}{promo}",
                width=meta.get("width"),
                height=meta.get("height"),
                duration=duration or None,
            )
        except TelegramRetryAfter as e:
            logger.warning("Telegram rate limit, ждём %ds", e.retry_after)
            await asyncio.sleep(e.retry_after)
            file = FSInputFile(result.file_path)
            sent = await message.answer_animation(
                animation=file, caption=f"{E['video']} {result.title}{promo}",
                width=meta.get("width"), height=meta.get("height"), duration=duration or None,
            )
        # Telegram может вернуть video вместо animation
        if sent.animation:
            return sent.animation.file_id
        if sent.video:
            return sent.video.file_id
        return None

    # фото
    try:
        sent = await message.answer_photo(
            photo=file,
            caption=f"{E['camera']} {result.title}{promo}",
        )
    except TelegramRetryAfter as e:
        logger.warning("Telegram rate limit, ждём %ds", e.retry_after)
        await asyncio.sleep(e.retry_after)
        file = FSInputFile(result.file_path)
        sent = await message.answer_photo(photo=file, caption=f"{E['camera']} {result.title}{promo}")
    return sent.photo[-1].file_id


async def _send_cached(message: Message, file_id: str, media_type: str) -> None:
    """Отправляет из кэша по file_id"""
    try:
        if media_type == "video":
            await message.answer_video(video=file_id, caption=f"{E['video']} Reddit Video")
        elif media_type == "gif":
            try:
                await message.answer_animation(animation=file_id, caption=f"{E['video']} Reddit GIF")
            except Exception:
                # GIF мог быть сохранён как video
                await message.answer_video(video=file_id, caption=f"{E['video']} Reddit GIF")
        elif media_type == "photo":
            await message.answer_photo(photo=file_id, caption=f"{E['camera']} Reddit Photo")
    except Exception as e:
        logger.error("Ошибка отправки из кэша: %s", e)
        await message.answer(f"{E['warning']} Кэш устарел. Отправь ссылку ещё раз.")


def _get_error_text(error: str, lang: str = "ru") -> str:
    """Человеко-понятное сообщение об ошибке"""
    error_lower = error.lower()

    if "private" in error_lower or "login" in error_lower or "nsfw" in error_lower:
        return t("error.private", lang)
    elif "not found" in error_lower or "404" in error_lower or "deleted" in error_lower:
        return t("error.not_found", lang)
    elif "unavailable" in error_lower:
        return t("error.unavailable", lang)
    elif "too large" in error_lower:
        return t("error.too_large", lang)
    elif "timeout" in error_lower:
        return t("error.timeout", lang)
    elif "403" in error_lower or "rate limit" in error_lower or "ip_blocked" in error_lower:
        return t("error.ip_blocked", lang)
    else:
        return t("error.generic", lang)


# === Алерты о падении источников ===

_bot_ref = None


def setup_fallback_alerts(bot) -> None:
    global _bot_ref
    _bot_ref = bot
    downloader.on_source_failed = _on_source_failed
    logger.info("Алерты о падении источников подключены")


def _on_source_failed(source: str, error: str) -> None:
    if _bot_ref is None:
        return
    try:
        asyncio.create_task(_send_fallback_alert(source, error))
    except RuntimeError:
        pass


async def _send_fallback_alert(source: str, error: str) -> None:
    now = time.time()
    category = classify_error(error)
    if category in _SILENT_CATEGORIES:
        return

    if source == "warp" and category == "ip_blocked":
        from bot.utils.docker import restart_warp
        restarted = await restart_warp()
        if restarted:
            logger.info("WARP перезапущен после ip_blocked")

    throttle_key = f"{source}:{category}"
    last = _last_fallback_alert.get(throttle_key, 0)
    if now - last < _FALLBACK_ALERT_THROTTLE:
        return
    _last_fallback_alert[throttle_key] = now

    short_error = error[:300] + "..." if len(error) > 300 else error
    category_label = _ERROR_CATEGORY_LABELS.get(category, category)

    warp_note = ""
    if source == "warp" and category == "ip_blocked":
        warp_note = "\n\n♻️ <i>WARP контейнер перезапущен для смены IP</i>"

    text = (
        f"{E['warning']} <b>Источник упал!</b>\n\n"
        f"<b>Источник:</b> {source}\n"
        f"<b>Категория:</b> {category_label}\n"
        f"<b>Ошибка:</b> <code>{short_error}</code>"
        f"{warp_note}"
    )

    for admin_id in settings.admin_id_list:
        try:
            await _bot_ref.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.warning("Не удалось уведомить админа %s: %s", admin_id, e)
