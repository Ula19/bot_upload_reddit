"""Сервис скачивания Reddit — видео, фото, GIF, галереи
Видео: yt-dlp (автоматический DASH merge video+audio, как YouTube бот).
Fallback: ручной HTTP + ffmpeg merge.
Галереи: Reddit JSON API (yt-dlp не умеет галереи).
Fallback chain: direct → SOCKS5 прокси → WARP.
"""
import asyncio
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable

import aiohttp

from bot.config import settings

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = settings.max_file_size
WARP_PROXY = "socks5://warp:9091"

# User-Agent для Reddit API (Reddit блокирует дефолтные)
REDDIT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"


@dataclass
class MediaInfo:
    """Информация о медиа в Reddit посте"""
    title: str
    media_type: str  # video, photo, gif, gallery
    # для видео: доступные качества {"240": 5, "360": 10, "480": 20, "720": 50, "1080": 100}
    qualities: dict | None = None
    # для галерей: список URL изображений
    gallery_urls: list[str] = field(default_factory=list)
    # для фото/gif: прямой URL
    direct_url: str | None = None
    # DASH video URL (Reddit отдаёт видео без аудио)
    dash_video_url: str | None = None
    # DASH audio URL кандидаты (Reddit меняет формат)
    dash_audio_urls: list[str] = field(default_factory=list)
    # суб-реддит и автор
    subreddit: str | None = None
    author: str | None = None
    # NSFW флаг
    is_nsfw: bool = False


@dataclass
class DownloadResult:
    """Результат скачивания"""
    file_path: str
    media_type: str       # video, photo, gif
    title: str
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    format_key: str = ""


class FileTooLargeError(Exception):
    """Файл превышает лимит Telegram (2 ГБ)"""
    pass


class _ExternalLink(Exception):
    """Пост содержит внешнюю ссылку (imgur/gfycat/redgifs) — нужен yt-dlp"""
    pass


def classify_error(error_msg: str) -> str:
    """Классифицирует ошибку в категорию для алертов."""
    msg = error_msg.lower()
    if "403" in msg or "forbidden" in msg or "blocked" in msg or "rate limit" in msg:
        return "ip_blocked"
    if "login" in msg or "nsfw" in msg or "private" in msg or "quarantine" in msg:
        return "auth_required"
    if "timeout" in msg or "connection" in msg or "unreachable" in msg or "socks" in msg:
        return "network"
    if "unavailable" in msg or "not found" in msg or "404" in msg or "deleted" in msg:
        return "unavailable"
    return "unknown"


class RedditDownloader:
    """Скачивает медиа из Reddit.
    Fallback chain: direct → SOCKS5 прокси → WARP.
    """

    def __init__(self):
        self.download_dir = tempfile.mkdtemp(prefix="reddit_bot_")
        self._proxy = settings.proxy_url or None
        self.on_source_failed: Callable[[str, str], None] | None = None

        # Fallback-цепочка: direct → SOCKS5 proxy → WARP
        logger.info("Direct (PRIMARY): без прокси")
        if self._proxy:
            logger.info("Резидентный SOCKS5 прокси (fallback): %s", self._proxy)
        logger.info("WARP прокси (последний шанс): %s", WARP_PROXY)

    def _fire_source_failed(self, source: str, error: Exception) -> None:
        if self.on_source_failed is None:
            return
        try:
            self.on_source_failed(source, str(error))
        except Exception as e:
            logger.warning("on_source_failed callback упал: %s", e)

    def _cleanup_old_files(self, max_age_minutes: int = 30) -> None:
        now = time.time()
        cutoff = now - max_age_minutes * 60
        try:
            for filename in os.listdir(self.download_dir):
                filepath = os.path.join(self.download_dir, filename)
                if os.path.isfile(filepath) and os.path.getmtime(filepath) < cutoff:
                    os.remove(filepath)
                    logger.info("Очистка старого файла: %s", filename)
        except OSError as e:
            logger.warning("Ошибка при очистке: %s", e)

    # === Proxy opts для yt-dlp ===

    def _warp_opts(self) -> dict:
        return {
            "quiet": True,
            "no_warnings": True,
            "proxy": WARP_PROXY,
            "socket_timeout": 30,
            "retries": 3,
        }

    def _proxy_opts(self) -> dict:
        return {
            "quiet": True,
            "no_warnings": True,
            "proxy": self._proxy,
            "socket_timeout": 30,
            "retries": 3,
        }

    def _direct_opts(self) -> dict:
        return {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "retries": 2,
        }

    # === Получение метаданных ===

    async def get_info(self, url: str) -> MediaInfo | None:
        """Получает метаданные поста Reddit.
        Сначала пробуем Reddit JSON API, потом yt-dlp (только для внешних ссылок).
        Возвращает None если пост без медиа (текстовый).
        """
        t_start = time.monotonic()
        need_ytdlp = False  # пост с внешней ссылкой — нужен yt-dlp

        # попробуем JSON API — быстрее и даёт галереи
        for source, proxy in self._proxy_chain_http():
            try:
                info = await self._get_info_json(url, proxy)
                if info:
                    elapsed = time.monotonic() - t_start
                    logger.info("[METRIC] get_info %.2fs source=%s type=%s", elapsed, source, info.media_type)
                    return info
                # JSON API ответил 200, но медиа в посте нет — текстовый пост
                logger.info("JSON API (%s): пост без медиа", source)
                return None
            except _ExternalLink as el:
                # пост содержит внешнюю ссылку — нужен yt-dlp
                logger.info("JSON API (%s): внешняя ссылка %s — пробуем yt-dlp", source, el)
                need_ytdlp = True
                break
            except Exception as e:
                cat = classify_error(str(e))
                if cat == "unavailable":
                    raise
                logger.warning("JSON API через %s не сработал: %s", source, e)
                self._fire_source_failed(source, e)

        # yt-dlp: либо JSON API не ответил, либо пост с внешней ссылкой
        if not need_ytdlp:
            # JSON API не ответил ни через один источник — yt-dlp как последний шанс
            logger.info("JSON API недоступен — пробуем yt-dlp напрямую")

        for source, opts in self._proxy_chain_ytdlp():
            try:
                info = await self._get_info_ytdlp(url, opts)
                if info:
                    elapsed = time.monotonic() - t_start
                    logger.info("[METRIC] get_info %.2fs source=%s (yt-dlp) type=%s", elapsed, source, info.media_type)
                    return info
            except Exception as e:
                cat = classify_error(str(e))
                if cat == "unavailable":
                    raise
                logger.warning("yt-dlp через %s не сработал: %s", source, e)
                self._fire_source_failed(source, e)

        raise RuntimeError("Не удалось получить информацию о посте")

    def _proxy_chain_http(self) -> list[tuple[str, str | None]]:
        """Цепочка прокси для HTTP-запросов: direct → SOCKS5 proxy → WARP"""
        chain = [("direct", None)]
        if self._proxy:
            chain.append(("proxy", self._proxy))
        chain.append(("warp", f"socks5://warp:9091"))
        return chain

    def _proxy_chain_ytdlp(self) -> list[tuple[str, dict]]:
        """Цепочка прокси для yt-dlp: direct → SOCKS5 proxy → WARP"""
        chain = [("direct", self._direct_opts())]
        if self._proxy:
            chain.append(("proxy", self._proxy_opts()))
        chain.append(("warp", self._warp_opts()))
        return chain

    async def _get_info_json(self, url: str, proxy: str | None) -> MediaInfo | None:
        """Получает инфо через Reddit JSON API (.json endpoint)"""
        # нормализуем URL для JSON API
        json_url = self._make_json_url(url)
        if not json_url:
            return None

        connector = None
        if proxy and proxy.startswith("socks5://"):
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(proxy)

        timeout = aiohttp.ClientTimeout(total=15)
        headers = {"User-Agent": REDDIT_USER_AGENT}

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(json_url, headers=headers, allow_redirects=True) as resp:
                if resp.status == 403:
                    raise RuntimeError("403 Forbidden — IP заблокирован Reddit")
                if resp.status == 404:
                    raise RuntimeError("404 — пост не найден или удалён")
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = await resp.json()

        return self._parse_reddit_json(data, url)

    def _make_json_url(self, url: str) -> str | None:
        """Строит .json URL для Reddit API"""
        url = url.rstrip("/")
        # reddit.com/r/xxx/comments/yyy/...
        match = re.search(r"(reddit\.com/r/\w+/comments/\w+(?:/\w+)?)", url)
        if match:
            return f"https://www.{match.group(1)}.json"
        # reddit.com/gallery/xxx
        match = re.search(r"reddit\.com/gallery/(\w+)", url)
        if match:
            return f"https://www.reddit.com/comments/{match.group(1)}.json"
        # redd.it/xxx (short link)
        match = re.search(r"redd\.it/(\w+)", url)
        if match:
            return f"https://www.reddit.com/comments/{match.group(1)}.json"
        # reddit.com/r/xxx/s/xxx (share link) — нужен редирект, JSON API не поддерживает
        return None

    def _parse_reddit_json(self, data: list | dict, original_url: str) -> MediaInfo | None:
        """Парсит ответ Reddit JSON API"""
        if isinstance(data, list) and len(data) > 0:
            post_data = data[0].get("data", {}).get("children", [{}])[0].get("data", {})
        elif isinstance(data, dict):
            children = data.get("data", {}).get("children", [])
            if not children:
                return None
            post_data = children[0].get("data", {})
        else:
            return None

        if not post_data:
            return None

        title = post_data.get("title", "Reddit Post")
        subreddit = post_data.get("subreddit", "")
        author = post_data.get("author", "")
        is_nsfw = post_data.get("over_18", False)
        is_video = post_data.get("is_video", False)

        # === Видео (Reddit hosted) ===
        if is_video and post_data.get("media"):
            reddit_video = post_data["media"].get("reddit_video", {})
            if reddit_video:
                dash_url = reddit_video.get("dash_url") or reddit_video.get("fallback_url", "")
                duration = reddit_video.get("duration", 0)
                width = reddit_video.get("width", 0)
                height = reddit_video.get("height", 0)

                # парсим доступные качества из fallback_url
                fallback = reddit_video.get("fallback_url", "")
                qualities = self._extract_video_qualities(fallback, duration)

                # audio URL — Reddit использует разные форматы, пробуем все
                base_url = re.sub(r"DASH_\d+\.mp4.*", "", fallback)
                audio_candidates = [
                    base_url + "DASH_AUDIO_128.mp4",
                    base_url + "DASH_audio.mp4",
                    base_url + "DASH_AUDIO_64.mp4",
                    base_url + "audio",
                ]
                audio_url = audio_candidates  # список кандидатов

                return MediaInfo(
                    title=title,
                    media_type="video",
                    qualities=qualities,
                    dash_video_url=fallback,
                    dash_audio_urls=audio_candidates,
                    subreddit=subreddit,
                    author=author,
                    is_nsfw=is_nsfw,
                )

        # === Кросспост — рекурсивно достаём оригинал ===
        crosspost_list = post_data.get("crosspost_parent_list", [])
        if crosspost_list:
            fake_data = [{"data": {"children": [{"data": crosspost_list[0]}]}}]
            result = self._parse_reddit_json(fake_data, original_url)
            if result:
                result.title = title  # сохраняем заголовок кросспоста
                return result

        # === Галерея ===
        if post_data.get("is_gallery") or "gallery_data" in post_data:
            gallery_urls = self._extract_gallery_urls(post_data)
            if gallery_urls:
                return MediaInfo(
                    title=title,
                    media_type="gallery",
                    gallery_urls=gallery_urls,
                    subreddit=subreddit,
                    author=author,
                    is_nsfw=is_nsfw,
                )

        # === Фото (одиночное) ===
        url_str = post_data.get("url", "") or post_data.get("url_overridden_by_dest", "")
        if url_str and re.search(r"\.(jpg|jpeg|png|webp)(\?.*)?$", url_str, re.IGNORECASE):
            return MediaInfo(
                title=title,
                media_type="photo",
                direct_url=url_str,
                subreddit=subreddit,
                author=author,
                is_nsfw=is_nsfw,
            )

        # === GIF / gifv ===
        if url_str and re.search(r"\.(gif|gifv)(\?.*)?$", url_str, re.IGNORECASE):
            # для .gifv (imgur) — mp4. Для .gif — пробуем найти mp4-вариант в preview
            direct = url_str.replace(".gifv", ".mp4")
            is_gifv = ".gifv" in url_str.lower()
            if not is_gifv:
                # Reddit для .gif часто имеет mp4-вариант в preview.variants.mp4
                preview = post_data.get("preview", {})
                if preview.get("images"):
                    variants = preview["images"][0].get("variants", {})
                    mp4_variant = variants.get("mp4", {})
                    mp4_url = mp4_variant.get("source", {}).get("url", "")
                    if mp4_url:
                        direct = mp4_url.replace("&amp;", "&")
            return MediaInfo(
                title=title,
                media_type="gif",
                direct_url=direct,
                subreddit=subreddit,
                author=author,
                is_nsfw=is_nsfw,
            )

        # === Reddit preview — animated (mp4-вариант) имеет приоритет над photo ===
        preview = post_data.get("preview", {})
        if preview.get("images"):
            # 1. Сначала пробуем animated preview (mp4) — это настоящий GIF
            variants = preview["images"][0].get("variants", {})
            mp4_variant = variants.get("mp4", {})
            if mp4_variant.get("source", {}).get("url"):
                mp4_url = mp4_variant["source"]["url"].replace("&amp;", "&")
                return MediaInfo(
                    title=title,
                    media_type="gif",
                    direct_url=mp4_url,
                    subreddit=subreddit,
                    author=author,
                    is_nsfw=is_nsfw,
                )
            # 2. Иначе статическое фото
            source_url = preview["images"][0].get("source", {}).get("url", "")
            if source_url:
                source_url = source_url.replace("&amp;", "&")
                return MediaInfo(
                    title=title,
                    media_type="photo",
                    direct_url=source_url,
                    subreddit=subreddit,
                    author=author,
                    is_nsfw=is_nsfw,
                )

        # === Внешняя ссылка (imgur, gfycat и т.п.) — нужен yt-dlp ===
        if url_str and ("imgur.com" in url_str or "gfycat.com" in url_str or "redgifs.com" in url_str
                        or "streamable.com" in url_str):
            raise _ExternalLink(url_str)

        # нет медиа (текстовый пост)
        return None

    def _extract_video_qualities(self, fallback_url: str, duration: int = 0) -> dict:
        """Извлекает список качеств из fallback URL Reddit видео"""
        qualities = {}
        # Reddit обычно имеет: 240, 360, 480, 720, 1080
        for height in [240, 360, 480, 720, 1080]:
            test_url = re.sub(r"DASH_\d+", f"DASH_{height}", fallback_url)
            # примерный размер на основе битрейта
            # Reddit ~1.5 Мбит/с для 720p, ~0.5 для 360p
            bitrate_map = {240: 400, 360: 700, 480: 1200, 720: 2500, 1080: 5000}
            bitrate_kbps = bitrate_map.get(height, 1000)
            size_mb = int(bitrate_kbps * duration / 8 / 1024) if duration else 0
            qualities[str(height)] = max(size_mb, 1) if size_mb > 0 else 0
        return qualities

    def _extract_gallery_urls(self, post_data: dict) -> list[str]:
        """Извлекает URL изображений из галереи Reddit"""
        gallery_data = post_data.get("gallery_data", {})
        media_metadata = post_data.get("media_metadata", {})
        if not gallery_data or not media_metadata:
            return []

        items = gallery_data.get("items", [])
        urls = []
        for item in items:
            media_id = item.get("media_id", "")
            meta = media_metadata.get(media_id, {})
            if meta.get("status") != "valid":
                continue

            # берём оригинал (source)
            source = meta.get("s", {})
            url = source.get("u") or source.get("gif") or source.get("mp4")
            if url:
                # Reddit HTML-энкодит URL
                url = url.replace("&amp;", "&")
                urls.append(url)

        return urls

    async def _get_info_ytdlp(self, url: str, opts: dict) -> MediaInfo | None:
        """Получает инфо через yt-dlp (для внешних хостингов)"""
        import yt_dlp

        ydl_opts = {
            **opts,
            "skip_download": True,
            "ignore_no_formats_error": True,
        }

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, self._extract_info, url, ydl_opts)

        if not info:
            return None

        # определяем тип медиа
        formats = info.get("formats", [])
        has_video = any(f.get("vcodec", "none") != "none" for f in formats)

        if has_video:
            qualities = self._parse_ytdlp_qualities(info)
            return MediaInfo(
                title=info.get("title", "Reddit Media"),
                media_type="video",
                qualities=qualities,
            )

        # одиночное изображение/gif
        ext = info.get("ext", "")
        if ext in ("gif", "mp4"):
            return MediaInfo(
                title=info.get("title", "Reddit GIF"),
                media_type="gif",
                direct_url=info.get("url"),
            )

        if ext in ("jpg", "jpeg", "png", "webp"):
            return MediaInfo(
                title=info.get("title", "Reddit Photo"),
                media_type="photo",
                direct_url=info.get("url"),
            )

        return None

    def _parse_ytdlp_qualities(self, info: dict) -> dict:
        """Парсит качества из yt-dlp info (аналогично YouTube боту)"""
        formats = info.get("formats", [])
        duration = info.get("duration", 0) or 0
        target_heights = [240, 360, 480, 720, 1080]
        result = {}

        for h in target_heights:
            for fmt in formats:
                fmt_h = fmt.get("height") or 0
                if fmt_h != h:
                    continue
                if fmt.get("vcodec", "none") == "none":
                    continue
                size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
                if not size and fmt.get("tbr") and duration:
                    size = int(fmt["tbr"] * 1000 / 8 * duration)
                total_mb = int(size / 1024 / 1024) if size else 0
                result[str(h)] = max(total_mb, 1) if total_mb > 0 else 0
                break

        if not result:
            result = {"360": 0, "720": 0}
        return result

    def _extract_info(self, url: str, opts: dict) -> dict | None:
        import yt_dlp
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    # === Скачивание видео ===

    async def download_video(self, url: str, quality: str = "720",
                             info: MediaInfo | None = None) -> DownloadResult:
        """Скачивает Reddit видео с мержем аудио через ffmpeg"""
        self._cleanup_old_files()
        t_start = time.monotonic()

        if info and info.dash_video_url:
            # Reddit hosted — скачиваем video + audio отдельно, мержим
            result = await self._download_reddit_video(url, quality, info)
        else:
            # внешний хостинг — через yt-dlp
            result = await self._download_via_ytdlp(url, quality)

        checked = self._check_size(result)
        elapsed = time.monotonic() - t_start
        try:
            size_mb = os.path.getsize(result.file_path) / 1024 / 1024
        except OSError:
            size_mb = 0
        logger.info("[METRIC] download_video %.2fs quality=%s size=%.1fMB", elapsed, quality, size_mb)
        return checked

    async def _download_reddit_video(self, url: str, quality: str,
                                     info: MediaInfo) -> DownloadResult:
        """Скачивает Reddit видео через yt-dlp (как YouTube бот).
        yt-dlp нативно поддерживает Reddit DASH: сам находит video+audio потоки
        и мержит в mp4. Надёжнее ручного HTTP + ffmpeg.
        Fallback: ручной HTTP + ffmpeg merge (если yt-dlp не справился).
        """
        # === 1. PRIMARY: yt-dlp (автоматический DASH merge) ===
        try:
            result = await self._download_reddit_via_ytdlp(url, quality, info.title)
            logger.info("Reddit видео скачано через yt-dlp (auto merge)")
            return result
        except Exception as e:
            logger.warning("yt-dlp не смог скачать Reddit видео: %s", e)

        # === 2. FALLBACK: ручной HTTP + ffmpeg merge ===
        logger.info("Fallback: ручной HTTP + ffmpeg merge")
        return await self._download_reddit_video_manual(url, quality, info)

    async def _download_reddit_via_ytdlp(self, url: str, quality: str,
                                          title: str) -> DownloadResult:
        """Скачивает Reddit видео через yt-dlp с автоматическим DASH merge.
        Подход аналогичен YouTube боту: format selection + merge_output_format.
        """
        import yt_dlp

        height = int(quality)
        output_template = os.path.join(self.download_dir, f"%(id)s_{quality}p.%(ext)s")

        # h264 приоритет для совместимости с Telegram (как в YouTube боте)
        format_str = (
            f"bestvideo[height<={height}][vcodec~='^(avc|h264)']+bestaudio[ext=m4a]"
            f"/bestvideo[height<={height}]+bestaudio"
            f"/best[height<={height}]"
            f"/best"
        )

        for source, opts in self._proxy_chain_ytdlp():
            ydl_opts = {
                **opts,
                "format": format_str,
                "outtmpl": output_template,
                "merge_output_format": "mp4",
            }
            try:
                loop = asyncio.get_event_loop()
                info = await loop.run_in_executor(
                    None, self._download_ytdlp, url, ydl_opts
                )
                file_path = self._find_downloaded_file(info, "mp4")
                if file_path and os.path.exists(file_path):
                    return DownloadResult(
                        file_path=file_path,
                        media_type="video",
                        title=title or info.get("title", "Reddit Video"),
                        duration=info.get("duration"),
                        width=info.get("width"),
                        height=info.get("height"),
                        format_key=f"video_{quality}",
                    )
            except Exception as e:
                cat = classify_error(str(e))
                logger.warning("yt-dlp Reddit через %s: %s", source, e)
                self._fire_source_failed(source, e)
                if cat == "unavailable":
                    raise

        raise RuntimeError("yt-dlp не смог скачать Reddit видео через все источники")

    async def _download_reddit_video_manual(self, url: str, quality: str,
                                             info: MediaInfo) -> DownloadResult:
        """Ручной HTTP + ffmpeg merge (fallback если yt-dlp не справился).
        Скачивает video и audio отдельно, мержит через ffmpeg.
        """
        video_url = re.sub(r"DASH_\d+", f"DASH_{quality}", info.dash_video_url)
        ts = int(time.time())
        video_path = os.path.join(self.download_dir, f"video_{ts}.mp4")
        audio_path = os.path.join(self.download_dir, f"audio_{ts}.mp4")
        output_path = os.path.join(self.download_dir, f"merged_{ts}.mp4")

        # 1. Скачиваем ВИДЕО через прокси-цепочку
        video_ok = False
        for source, proxy in self._proxy_chain_http():
            try:
                await self._download_file(video_url, video_path, proxy)
                video_ok = True
                logger.info("Видео скачано через %s", source)
                break
            except Exception as e:
                logger.warning("Видео через %s не удалось: %s", source, e)
                self._fire_source_failed(source, e)
                if os.path.exists(video_path):
                    os.remove(video_path)

        if not video_ok:
            raise RuntimeError("download_failed")

        # 2. Скачиваем АУДИО — пробуем все URL-кандидаты через прокси-цепочку
        audio_ok = False
        for audio_url in info.dash_audio_urls:
            for source, proxy in self._proxy_chain_http():
                try:
                    await self._download_file(audio_url, audio_path, proxy)
                    if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
                        with open(audio_path, "rb") as f:
                            header = f.read(16)
                        if b"<!DOCTYPE" not in header and b"<html" not in header:
                            audio_ok = True
                            logger.info("Аудио скачано через %s: %s", source, audio_url.split("/")[-1])
                            break
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                except Exception:
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
            if audio_ok:
                break

        if not audio_ok:
            logger.warning("Аудио-дорожка не найдена — видео будет без звука")

        # 3. Мержим через ffmpeg (copy обоих потоков — без перекодирования)
        if audio_ok:
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", audio_path,
                "-c:v", "copy", "-c:a", "copy",
                "-movflags", "+faststart",
                output_path,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.warning("ffmpeg merge failed: %s", stderr.decode()[:300])
                output_path = video_path
        else:
            output_path = video_path

        # чистим временные
        for p in (video_path, audio_path):
            if p != output_path and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

        if not os.path.exists(output_path):
            raise RuntimeError("Не удалось создать видеофайл")

        return DownloadResult(
            file_path=output_path,
            media_type="video",
            title=info.title,
            format_key=f"video_{quality}",
        )

    async def _download_via_ytdlp(self, url: str, quality: str = "720") -> DownloadResult:
        """Скачивает через yt-dlp (для внешних хостингов: imgur, gfycat, redgifs)"""
        import yt_dlp

        output_template = os.path.join(self.download_dir, f"%(id)s_{quality}p.%(ext)s")
        height = int(quality)
        # h264 приоритет для совместимости с Telegram (как в YouTube боте)
        format_str = (
            f"bestvideo[height<={height}][vcodec~='^(avc|h264)']+bestaudio[ext=m4a]"
            f"/bestvideo[height<={height}]+bestaudio"
            f"/best[height<={height}]"
            f"/best"
        )

        for source, opts in self._proxy_chain_ytdlp():
            ydl_opts = {
                **opts,
                "format": format_str,
                "outtmpl": output_template,
                "merge_output_format": "mp4",
            }
            try:
                loop = asyncio.get_event_loop()
                info = await loop.run_in_executor(
                    None, self._download_ytdlp, url, ydl_opts
                )
                file_path = self._find_downloaded_file(info, "mp4")
                if file_path and os.path.exists(file_path):
                    return DownloadResult(
                        file_path=file_path,
                        media_type="video",
                        title=info.get("title", "Reddit Video"),
                        duration=info.get("duration"),
                        width=info.get("width"),
                        height=info.get("height"),
                        format_key=f"video_{quality}",
                    )
            except Exception as e:
                logger.warning("yt-dlp через %s не сработал: %s", source, e)
                self._fire_source_failed(source, e)
                if classify_error(str(e)) == "unavailable":
                    raise

        raise RuntimeError("download_failed")

    # === Скачивание фото/GIF ===

    async def download_photo(self, url: str, info: MediaInfo) -> DownloadResult:
        """Скачивает фото или GIF по прямой ссылке.
        Для mp4 (gif) применяем -movflags +faststart — Reddit preview часто
        имеет moov-атом в конце, Telegram не может стримить → чёрный экран.
        """
        self._cleanup_old_files()
        direct_url = info.direct_url
        if not direct_url:
            raise RuntimeError("Нет прямой ссылки на медиа")

        # определяем расширение из URL (а не из media_type)
        ext = "jpg"
        url_lower = direct_url.lower().split("?")[0]
        if url_lower.endswith(".mp4"):
            ext = "mp4"
        elif url_lower.endswith(".gif"):
            ext = "gif"
        elif url_lower.endswith(".png"):
            ext = "png"
        elif url_lower.endswith(".webp"):
            ext = "webp"
        elif url_lower.endswith((".jpg", ".jpeg")):
            ext = "jpg"
        elif info.media_type == "gif":
            ext = "mp4"  # fallback для gif без явного расширения

        file_path = os.path.join(self.download_dir, f"media_{int(time.time())}.{ext}")

        for source, proxy in self._proxy_chain_http():
            try:
                await self._download_file(direct_url, file_path, proxy)
                break
            except Exception as e:
                logger.warning("Скачивание фото через %s не удалось: %s", source, e)
                self._fire_source_failed(source, e)
                if os.path.exists(file_path):
                    os.remove(file_path)
        else:
            raise RuntimeError("download_failed")

        # для mp4 — переносим moov в начало (иначе Telegram показывает чёрный экран)
        if ext == "mp4":
            normalized = await self._normalize_mp4(file_path)
            if normalized:
                file_path = normalized

        # для .gif — конвертим в mp4 для Telegram (Telegram плохо играет raw .gif)
        if ext == "gif":
            converted = await self._convert_gif_to_mp4(file_path)
            if converted:
                file_path = converted

        return self._check_size(DownloadResult(
            file_path=file_path,
            media_type=info.media_type,
            title=info.title,
            format_key=info.media_type,
        ))

    async def _convert_gif_to_mp4(self, input_path: str) -> str | None:
        """Конвертирует GIF в mp4 (h264+yuv420p) — Telegram лучше играет mp4."""
        output_path = input_path.rsplit(".", 1)[0] + ".mp4"
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264",
            "-preset", "veryfast",
            output_path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning("ffmpeg gif→mp4 failed: %s", stderr.decode()[:200])
                if os.path.exists(output_path):
                    os.remove(output_path)
                return None
            try:
                os.remove(input_path)
            except OSError:
                pass
            return output_path
        except Exception as e:
            logger.warning("ffmpeg gif→mp4 error: %s", e)
            return None

    async def _normalize_mp4(self, input_path: str) -> str | None:
        """Применяет -movflags +faststart к mp4 (moov в начало, без перекодирования).
        Нужно для Reddit preview mp4 — без этого Telegram показывает чёрный экран.
        Возвращает путь к нормализованному файлу или None если ffmpeg упал.
        """
        output_path = input_path.replace(".mp4", "_fast.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-c", "copy",
            "-movflags", "+faststart",
            output_path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning("ffmpeg faststart failed: %s", stderr.decode()[:200])
                if os.path.exists(output_path):
                    os.remove(output_path)
                return None

            # удаляем оригинал, используем нормализованный
            try:
                os.remove(input_path)
            except OSError:
                pass
            return output_path
        except Exception as e:
            logger.warning("ffmpeg normalize error: %s", e)
            return None

    # === Скачивание галереи ===

    async def download_gallery(self, info: MediaInfo) -> list[DownloadResult]:
        """Скачивает все изображения галереи"""
        self._cleanup_old_files()
        results = []

        for i, img_url in enumerate(info.gallery_urls):
            ext = "jpg"
            ext_match = re.search(r"\.(png|webp|gif|jpg|jpeg|mp4)(\?|$)", img_url, re.IGNORECASE)
            if ext_match:
                ext = ext_match.group(1).lower()

            file_path = os.path.join(self.download_dir, f"gallery_{int(time.time())}_{i}.{ext}")

            for source, proxy in self._proxy_chain_http():
                try:
                    await self._download_file(img_url, file_path, proxy)
                    break
                except Exception as e:
                    logger.warning("Галерея [%d] через %s: %s", i, source, e)
                    if os.path.exists(file_path):
                        os.remove(file_path)
            else:
                logger.warning("Не удалось скачать элемент галереи %d", i)
                continue

            # для mp4 — переносим moov в начало (чтобы Telegram показывал превью)
            if ext == "mp4":
                normalized = await self._normalize_mp4(file_path)
                if normalized:
                    file_path = normalized

            media_type = "gif" if ext == "mp4" else "photo"
            results.append(DownloadResult(
                file_path=file_path,
                media_type=media_type,
                title=f"{info.title} ({i + 1}/{len(info.gallery_urls)})",
                format_key=f"gallery_{i}",
            ))

        return results

    # === Утилиты ===

    async def _download_file(self, url: str, dest: str, proxy: str | None = None) -> None:
        """Скачивает файл по HTTP с опциональным прокси"""
        connector = None
        if proxy and proxy.startswith("socks5://"):
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(proxy)

        timeout = aiohttp.ClientTimeout(total=120)
        headers = {"User-Agent": REDDIT_USER_AGENT}

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                if resp.status == 403:
                    raise RuntimeError("403 Forbidden")
                if resp.status == 404:
                    raise RuntimeError("404 Not Found")
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")

                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        f.write(chunk)

    def _download_ytdlp(self, url: str, opts: dict) -> dict:
        import yt_dlp
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    def _find_downloaded_file(self, info: dict, expected_ext: str) -> str | None:
        video_id = info.get("id", "")
        for filename in os.listdir(self.download_dir):
            if video_id and video_id in filename and filename.endswith(f".{expected_ext}"):
                return os.path.join(self.download_dir, filename)
        for filename in sorted(os.listdir(self.download_dir), reverse=True):
            if filename.endswith(f".{expected_ext}"):
                return os.path.join(self.download_dir, filename)
        return None

    def _check_size(self, result: DownloadResult) -> DownloadResult:
        file_size = os.path.getsize(result.file_path)
        if file_size > MAX_FILE_SIZE:
            self._remove_file(result.file_path)
            raise FileTooLargeError(
                f"Файл слишком большой ({file_size / 1024 / 1024:.0f} МБ)"
            )
        return result

    def cleanup(self, result: DownloadResult) -> None:
        self._remove_file(result.file_path)

    def cleanup_many(self, results: list[DownloadResult]) -> None:
        for r in results:
            self._remove_file(r.file_path)

    def _remove_file(self, path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info("Удалён: %s", path)
        except OSError as e:
            logger.warning("Не удалось удалить файл: %s", e)


# глобальный экземпляр
downloader = RedditDownloader()
