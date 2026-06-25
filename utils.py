"""Utility functions for astrbot_plugin_music_share.

Unified music-link routing table, URL extraction, and HTML title parsing.
"""

import hashlib
import re
import time
from enum import Enum, auto
from typing import Optional, Tuple

import aiohttp
from bs4 import BeautifulSoup


class Platform(Enum):
    SPOTIFY = auto()
    APPLE_MUSIC = auto()
    YOUTUBE = auto()
    SOUNDCLOUD = auto()
    BILIBILI_AUDIO = auto()
    NETEASE = auto()
    QQ_MUSIC = auto()
    KUGOU = auto()
    KUWO = auto()
    MIGU = auto()
    QISHUI = auto()


# ── Unified routing table ────────────────────────────────────────────
# Each entry: (regex, platform)
# Order matters: more specific patterns first to avoid over-matching.
_ROUTES: list[tuple[re.Pattern, Platform]] = [
    # Spotify (with optional intl- prefix)
    (
        re.compile(
            r"https?://(?:www\.)?open\.spotify\.com/(?:intl-[a-z]{2,5}/)?"
            r"(?:track|album|playlist)/[a-zA-Z0-9]+",
            re.IGNORECASE,
        ),
        Platform.SPOTIFY,
    ),
    # Apple Music
    (
        re.compile(
            r"https?://music\.apple\.com/[a-z]{2,3}/(?:album|song|playlist)/"
            r"[^/?]+/\d+(?:\?i=\d+)?",
            re.IGNORECASE,
        ),
        Platform.APPLE_MUSIC,
    ),
    # Bilibili audio ONLY (au\d+), NOT normal /video/ links
    (
        re.compile(
            r"https?://(?:www\.)?bilibili\.com/audio/au\d+"
            r"|https?://b23\.tv/au\d+",
            re.IGNORECASE,
        ),
        Platform.BILIBILI_AUDIO,
    ),
    # YouTube (including music.youtube.com and youtu.be)
    (
        re.compile(
            r"https?://(?:music\.)?youtube\.com/(?:watch\?v=|v/|embed/)"
            r"[a-zA-Z0-9_-]+"
            r"|https?://youtu\.be/[a-zA-Z0-9_-]+",
            re.IGNORECASE,
        ),
        Platform.YOUTUBE,
    ),
    # SoundCloud
    (
        re.compile(
            r"https?://(?:www\.)?soundcloud\.com/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+",
            re.IGNORECASE,
        ),
        Platform.SOUNDCLOUD,
    ),
    # 网易云音乐 (包括 163cn.tv 短链接)
    (
        re.compile(
            r"https?://music\.163\.com/(?:#/)?(?:song|album|playlist)\?id=\d+"
            r"|https?://(?:www\.)?163cn\.tv/[a-zA-Z0-9]+",
            re.IGNORECASE,
        ),
        Platform.NETEASE,
    ),
    # QQ 音乐
    (
        re.compile(
            r"https?://(?:[a-zA-Z0-9]+\.)?y\.qq\.com/"
            r"(?:n/ryqq/songDetail|n/ryqq/playlist|n/ryqq/album)"
            r"|https?://i\.y\.qq\.com/",
            re.IGNORECASE,
        ),
        Platform.QQ_MUSIC,
    ),
    # 酷狗音乐
    (
        re.compile(
            r"https?://(?:[a-zA-Z0-9]+\.)?kugou\.com/",
            re.IGNORECASE,
        ),
        Platform.KUGOU,
    ),
    # 酷我音乐
    (
        re.compile(
            r"https?://(?:[a-zA-Z0-9]+\.)?kuwo\.cn/",
            re.IGNORECASE,
        ),
        Platform.KUWO,
    ),
    # 咪咕音乐
    (
        re.compile(
            r"https?://(?:[a-zA-Z0-9]+\.)?migu\.cn/",
            re.IGNORECASE,
        ),
        Platform.MIGU,
    ),
    # 汽水音乐 (抖音旗下)
    (
        re.compile(
            r"https?://(?:[a-zA-Z0-9]+\.)?qishui\.douyin\.com/.*"
            r"|https?://(?:www\.)?qishuiyinyue\.com/.*",
            re.IGNORECASE,
        ),
        Platform.QISHUI,
    ),
]


# ── Generic URL extraction ─────────────────────────────────────────
# Matches any http/https URL until whitespace or common delimiters
_URL_RE = re.compile(r"https?://[^\s<>\"{}|\\^`\[\]]+", re.IGNORECASE)


def extract_music_url(text: str) -> Optional[Tuple[str, Platform]]:
    """Extract the first music share URL from a message text.

    Strategy: first extract all URLs from the text with a generic regex,
    then match each URL against the platform routing table.
    This handles complex share formats like
    「绫华分享童可可的单曲《补妆》https://m.kugou.com/share/song.html?... (@酷狗音乐)」

    Returns (url, platform) if found, None otherwise.
    """
    for url_match in _URL_RE.finditer(text):
        url = url_match.group(0).rstrip(".,;:!?）)】』")
        for regex, platform in _ROUTES:
            if regex.search(url):
                return url, platform
    return None


def is_group_event(event) -> bool:
    """Check if an event is from a group chat."""
    try:
        event.get_group_id()
        return True
    except (AttributeError, NotImplementedError):
        return False


# ── Generic HTML title parser for domestic platforms ─────────────────
# These platforms don't have structured JSON-LD; we parse <title> and
# clean away platform suffixes.

_TITLE_CLEANERS: list[tuple[re.Pattern, str]] = [
    # Order matters: remove longer/more-specific first
    # Kugou: "童可可_童可可补妆高品质MV首发|童可可 - 酷狗音乐"
    #   → replace _ with " - " first, then strip Kugou-specific suffixes
    (re.compile(r"高品质MV首发\|.*$", re.IGNORECASE), ""),
    (re.compile(r"_\s*", re.IGNORECASE), " - "),
    (re.compile(r"\s*[-_—–]\s*高清在线试听\s*$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-_—–]\s*单曲\s*[-_—–]\s*网易云音乐\s*$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-_—–]\s*网易云音乐\s*$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-_—–]\s*QQ音乐\s*[-_—–]\s*.*$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-_—–]\s*QQ音乐\s*$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-_—–]\s*酷狗音乐\s*$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-_—–]\s*酷我音乐\s*$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-_—–]\s*咪咕音乐\s*$", re.IGNORECASE), ""),
    (re.compile(r"\s*[-_—–]\s*音乐\s*[-_—–]\s*.*$", re.IGNORECASE), ""),
    (re.compile(r"\s*在线试听\s*$", re.IGNORECASE), ""),
    (re.compile(r"\s*免费在线试听\s*$", re.IGNORECASE), ""),
    (re.compile(r"\s*MV\s*[-_—–]\s*.*$", re.IGNORECASE), ""),
]


async def parse_html_title(url: str, proxy: str = "") -> Optional[dict]:
    """Fetch a music platform page and extract title/artist/cover from HTML.

    Returns dict with keys: title, artist, cover_url, or None on failure.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        kwargs = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=15)}
        if proxy:
            kwargs["proxy"] = proxy
        async with aiohttp.ClientSession() as session:
            async with session.get(url, **kwargs) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
    except Exception:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # 1. Structured meta
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        raw = str(og_title["content"]).strip()
    else:
        title_tag = soup.find("title")
        raw = title_tag.string.strip() if title_tag and title_tag.string else ""
    if not raw:
        return None

    # 1b. Extract cover image from og:image
    cover_url = ""
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        cover_url = str(og_image["content"]).strip()

    # 2. Clean platform suffixes
    clean = raw
    for regex, replacement in _TITLE_CLEANERS:
        clean = regex.sub(replacement, clean).strip()
    if not clean:
        clean = raw  # fallback to raw if cleaning removed everything

    # 3. Split into song / artist
    song_name, artist = _split_title(clean)
    return {
        "title": song_name or clean,
        "artist": artist or "",
        "cover_url": cover_url,
    }


def _split_title(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Try to split a cleaned title into (song, artist)."""
    # "Song Name - Artist"
    for sep in (" - ", " – ", " — ", " | ", " / "):
        if sep in raw:
            parts = raw.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    # "Artist Song Name" (no separator) — return as-is
    return raw.strip(), None


class ResultCache:
    """Simple in-memory cache with TTL (time-to-live)."""

    def __init__(self, ttl_seconds: int = 600):
        self._cache: dict[str, tuple[object, float]] = {}
        self._ttl = ttl_seconds

    def _key(self, url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()

    def get(self, url: str) -> Optional[object]:
        key = self._key(url)
        entry = self._cache.get(key)
        if entry is None:
            return None
        value, timestamp = entry
        if time.time() - timestamp > self._ttl:
            del self._cache[key]
            return None
        return value

    def set(self, url: str, value: object) -> None:
        self._cache[self._key(url)] = (value, time.time())