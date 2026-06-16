"""
astrbot_plugin_anymusic - AnyMusic 音乐插件

自动识别群聊中的主流音乐分享链接，解析元数据并双引擎竞争下载。
"""

import asyncio
import json
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import File, Image, Record, file_to_base64
from astrbot.api.star import Context, Star, StarTools, register

from .config import ConfigHelper
from .cover_card import make_info_card
from .downloader import MusicDownloader
from .parsers.apple_music import AppleMusicParser, SongInfo
from .parsers.spotify import SpotifyParser
from .utils import (
    Platform,
    ResultCache,
    extract_music_url,
    is_group_event,
    parse_html_title,
)


@register("astrbot_plugin_anymusic", "user", "AnyMusic", "1.0.0")
class MusicSharePlugin(Star):
    """Auto-detect music links & LLM song search tool."""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self.config_helper = ConfigHelper(config)

        self.apple_parser = AppleMusicParser()
        self.spotify_parser = SpotifyParser()
        self.downloader = MusicDownloader(self.config_helper)
        self._cache = ResultCache(ttl_seconds=600)

    # ── LLM Tool: search_song ────────────────────────────────────────────

    @filter.llm_tool(name="search_song")
    async def search_song(self, event: AstrMessageEvent, song_name: str):
        '''在 YouTube 上搜索指定歌曲，下载音频并发送语音消息和文件到当前群聊。
        当用户说我想听XXX、放一首XXX、点歌XXX、搜一下XXX时使用此工具。

        Args:
            song_name(string): 歌曲名称，最好包含艺术家名以提高搜索准确性，如 周杰伦 晴天
        '''
        logger.info(f"[MusicShare] LLM 点歌: '{song_name}'")

        # Try to get cover art and send a card first
        if info := await self._resolve_llm_song_info(song_name):
            async for result in self._send_cover_card(event, info):
                yield result
            async for result in self._download_then_send(
                event, info.title, info.artist,
                expected_duration=info.duration or "",
            ):
                yield result
        else:
            async for result in self._download_then_send(
                event, song_name, "", expected_duration=""
            ):
                yield result

    # ── Auto-detect music links ───────────────────────────────────────────

    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """Check every message for music share links from all supported platforms."""
        if is_group_event(event):
            try:
                group_id = str(event.get_group_id())
            except Exception:
                group_id = ""
            if group_id and not self.config_helper.is_group_enabled(group_id):
                return

        message_text = event.message_str or ""
        result = extract_music_url(message_text)
        if result is None:
            return
        url, platform = result

        logger.info(f"[MusicShare] 检测到 {platform.name} 链接: {url}")

        # Check cache
        song_info = self._cache.get(url)
        if song_info is None:
            song_info = await self._resolve_song_info(url, platform)
            if song_info is None:
                yield event.plain_result("无法识别该音乐链接")
                return
            self._cache.set(url, song_info)

        logger.info(
            f"[MusicShare] 解析成功 [{platform.name}]: "
            f"{song_info.title} - {song_info.artist}"
        )

        # Send cover card
        if song_info.cover_url:
            async for result in self._send_cover_card(event, song_info):
                yield result

        # Download and send audio (dual-engine competition for ALL platforms)
        async for result in self._download_then_send(
            event, song_info.title, song_info.artist,
            expected_duration=song_info.duration,
        ):
            yield result

    # ── Song info resolution ─────────────────────────────────────────────

    async def _resolve_song_info(self, url: str, platform: Platform) -> Optional[SongInfo]:
        """Resolve song metadata based on the platform type."""
        proxy = self.config_helper.proxy() or ""

        # ── Structured parsers (Spotify / Apple Music) ──
        if platform == Platform.SPOTIFY:
            return await self.spotify_parser.parse(url)
        if platform == Platform.APPLE_MUSIC:
            return await self.apple_parser.parse(url)

        # ── URL-direct platforms: use yt-dlp --dump-json ──
        if platform in (Platform.YOUTUBE, Platform.SOUNDCLOUD, Platform.BILIBILI_AUDIO):
            return await self._resolve_via_ytdlp(url)

        # ── Domestic platforms: parse HTML <title> ──
        if platform in (
            Platform.NETEASE, Platform.QQ_MUSIC,
            Platform.KUGOU, Platform.KUWO, Platform.MIGU,
            Platform.QISHUI,
        ):
            data = await parse_html_title(url, proxy)
            if data:
                return SongInfo(
                    title=data["title"],
                    artist=data.get("artist", ""),
                    cover_url=data.get("cover_url", ""),
                    source=platform.name.lower(),
                )
            return None

        return None

    async def _resolve_via_ytdlp(self, url: str) -> Optional[SongInfo]:
        """Extract song metadata from a URL via yt-dlp --dump-json."""
        try:
            python_exe = self._find_python()
            if not python_exe:
                return None
            cmd = [
                python_exe, "-m", "yt_dlp",
                url,
                "--dump-json",
                "--no-warnings",
                "--skip-download",
                "--quiet",
                "--no-playlist",
            ]
            proxy = self.config_helper.proxy()
            if proxy:
                cmd.extend(["--proxy", proxy])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config_helper.search_timeout(),
            )
            if proc.returncode != 0:
                return None
            data = json.loads(stdout.decode("utf-8", errors="replace"))
            dur = data.get("duration", 0) or 0
            dur_str = self._format_duration(dur) if dur else ""
            return SongInfo(
                title=data.get("title", ""),
                artist=data.get("uploader", "") or data.get("artist", ""),
                duration=dur_str,
                cover_url=data.get("thumbnail", ""),
                source=Platform.YOUTUBE.name.lower(),
            )
        except Exception as e:
            logger.warning(f"[MusicShare] yt-dlp metadata extract failed: {e}")
            return None

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format seconds as M:SS or H:MM:SS."""
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    @staticmethod
    def _find_python() -> Optional[str]:
        import sys
        try:
            return sys.executable
        except Exception:
            return None

    async def _resolve_llm_song_info(self, song_name: str) -> Optional[SongInfo]:
        """Quick metadata lookup via yt-dlp to get title/artist/cover for cards."""
        try:
            python_exe = self._find_python()
            if not python_exe:
                return None
            cmd = [
                python_exe, "-m", "yt_dlp",
                f"ytsearch1:{song_name}",
                "--dump-json", "--skip-download",
                "--quiet", "--no-playlist",
                "--no-warnings",
            ]
            proxy = self.config_helper.proxy()
            if proxy:
                cmd.extend(["--proxy", proxy])
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.config_helper.search_timeout(),
            )
            if proc.returncode != 0:
                return None
            data = json.loads(stdout.decode("utf-8", errors="replace"))
            return SongInfo(
                title=data.get("title", song_name),
                artist=data.get("uploader", "") or data.get("artist", "") or "",
                cover_url=data.get("thumbnail", "") or "",
                source="llm_search",
            )
        except Exception:
            return None

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _send_cover_card(self, event: AstrMessageEvent, song_info: SongInfo):
        """Generate and send the info card as an image."""
        try:
            proxy = self.config_helper.proxy() or ""
            card = await make_info_card(
                song_info.cover_url,
                song_info.title,
                song_info.artist,
                song_info.album,
                song_info.source,
                proxy,
                duration=song_info.duration,
                release_date=song_info.release_date,
            )
            temp_dir = Path(self.config_helper.download_dir() or "data/music")
            temp_dir.mkdir(parents=True, exist_ok=True)
            safe_name = song_info.title[:20].replace("/", "_")
            card_path = temp_dir / f"cover_{safe_name}.png"
            card.save(str(card_path), "PNG")
            yield event.set_result(
                event.chain_result([Image.fromFileSystem(str(card_path))])
            )
            card_path.unlink()
        except Exception as e:
            logger.error(f"[MusicShare] 封面卡片生成失败: {e}")
            yield event.plain_result(
                f"歌名: {song_info.title}\n艺术家: {song_info.artist}\n来源: {song_info.source}"
            )

    async def _download_then_send(
        self, event: AstrMessageEvent, title: str, artist: str,
        expected_duration: str = "",
    ):
        """Download audio and send voice + file.

        If the platform does not support Record (voice) messages,
        automatically falls back to sending as a File.
        """
        download_dir = Path(StarTools.get_data_dir("music_share")) / "downloads"
        download_dir.mkdir(parents=True, exist_ok=True)

        audio_file, error_msg = await self.downloader.search_and_download(
            title, artist,
            expected_duration=expected_duration,
            match_threshold=self.config_helper.match_threshold(),
            download_dir=download_dir,
        )

        if not audio_file:
            yield event.plain_result(error_msg or f"未找到匹配的歌曲: {title} {artist}".strip())
            return

        file_size_mb = audio_file.stat().st_size / (1024 * 1024)
        if file_size_mb > self.config_helper.max_file_size_mb():
            self.downloader.clean_file(audio_file)
            yield event.plain_result(f"音频文件过大 ({file_size_mb:.1f}MB)")
            return

        try:
            mode = self.config_helper.send_mode()
            record_sent = False

            if mode in ("仅语音", "都发送"):
                try:
                    record = Record.fromFileSystem(str(audio_file))
                    yield event.set_result(event.chain_result([record]))
                    record_sent = True
                except Exception as e:
                    logger.warning(
                        f"[MusicShare] Record 发送失败，平台可能不支持语音消息: {e}"
                    )
                    if mode == "仅语音":
                        logger.info("[MusicShare] 回退为发送文件")
                        try:
                            file_b64 = file_to_base64(str(audio_file))
                            file_component = File(name=audio_file.name, url=file_b64)
                            yield event.set_result(event.chain_result([file_component]))
                            record_sent = True
                        except Exception as fe:
                            logger.error(f"[MusicShare] 文件发送也失败: {fe}")
                            yield event.plain_result(f"发送失败: {fe}")
                    else:
                        logger.info("[MusicShare] Record 失败但仍会尝试发送 File")

            if mode in ("仅文件", "都发送"):
                try:
                    file_b64 = file_to_base64(str(audio_file))
                    file_component = File(name=audio_file.name, url=file_b64)
                    yield event.set_result(event.chain_result([file_component]))
                    record_sent = True
                except Exception as e:
                    logger.error(f"[MusicShare] File 发送失败: {e}")
                    if not record_sent:
                        yield event.plain_result(f"发送失败: {e}")

            logger.info(f"[MusicShare] 已发送: {audio_file.name} (mode={mode})")
        except Exception as e:
            logger.error(f"[MusicShare] 发送失败: {e}")
            yield event.plain_result(f"发送失败: {e}")

        self.downloader.clean_file(audio_file)