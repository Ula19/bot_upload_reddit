"""Утилиты и вспомогательные функции"""
import re


# паттерны Reddit ссылок
_REDDIT_PATTERNS = [
    r"https?://(www\.)?reddit\.com/r/\w+/comments/\w+",
    r"https?://(old\.)?reddit\.com/r/\w+/comments/\w+",
    r"https?://(www\.)?reddit\.com/gallery/\w+",
    r"https?://i\.redd\.it/\w+",
    r"https?://v\.redd\.it/\w+",
    r"https?://redd\.it/\w+",
    r"https?://(www\.)?reddit\.com/r/\w+/s/\w+",
]


def is_reddit_url(text: str) -> bool:
    """Проверяет, является ли текст ссылкой на Reddit"""
    text = text.strip()
    return any(re.match(pattern, text) for pattern in _REDDIT_PATTERNS)


def clean_reddit_url(url: str) -> str:
    """Очищает URL — убирает query-параметры и мусор"""
    url = url.strip()

    # убираем query params (?utm_source=..., ?share_id=... и т.п.)
    url = re.split(r"[?#]", url)[0].rstrip("/")

    return url


def extract_post_id(url: str) -> str | None:
    """Извлекает post ID из Reddit URL"""
    patterns = [
        r"reddit\.com/r/\w+/comments/(\w+)",
        r"reddit\.com/gallery/(\w+)",
        r"redd\.it/(\w+)",
        r"v\.redd\.it/(\w+)",
        r"i\.redd\.it/(\w+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None
