"""
astrbot_plugin_anymusic - AnyMusic 音乐插件

自动识别群聊中的主流音乐分享链接，解析元数据并双引擎竞争下载。
"""

import asyncio
import json
import os
import re as _re
import time
import uuid
from pathlib import Path
from typing import Optional

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import (
    ComponentType,
    File,
    Image,
    Record,
)
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.io import file_to_base64

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


@register("astrbot_plugin_anymusic", "user", "AnyMusic", "1.0.3")
class MusicSharePlugin(Star):
    """Auto-detect music links & LLM song search / voice recognition tool."""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config
        self.config_helper = ConfigHelper(config)

        self.apple_parser = AppleMusicParser()
        self.spotify_parser = SpotifyParser()
        self.downloader = MusicDownloader(self.config_helper)
        self._cache = ResultCache(ttl_seconds=600)
        # Voice cache per group: {"url": str, "path": str, "ts": float}
        self._voice_cache: dict[str, dict] = {}
        self._voice_cache_ttl = 120

    # ── LLM Tool: search_song ────────────────────────────────────────────

    @filter.llm_tool(name="search_song")
    async def search_song(self, event: AstrMessageEvent, song_name: str):
        '''在 YouTube 上搜索指定歌曲，下载音频并发送语音消息和文件到当前群聊。
        当用户说我想听XXX、放一首XXX、点歌XXX、搜一下XXX时使用此工具。

        Args:
            song_name(string): 歌曲名称，最好包含艺术家名以提高搜索准确性，如 周杰伦 晴天
        '''
        logger.info(f"[MusicShare] LLM 点歌: '{song_name}'")

        mode = self.config_helper.llm_tool_mode()
        info = None
        if mode != "仅语音":
            info = await self._resolve_llm_song_info(song_name)

        if info and "图片" in mode:
            async for result in self._send_cover_card(event, info):
                yield result

        song_title = info.title if info else song_name
        song_artist = info.artist if info else ""
        song_dur = info.duration if info else ""

        async for result in self._download_then_send(
            event, song_title, song_artist,
            expected_duration=song_dur or "",
            force_mode=(mode if mode == "都发送" else None),
        ):
            yield result

    # ── LLM Tool: recognize_song ─────────────────────────────────────────

    @filter.llm_tool(name="recognize_song")
    async def recognize_song(self, event: AstrMessageEvent):
        '''听歌识曲工具。当用户发送语音消息并想识别其中的歌曲时使用此工具。
        适用场景：用户说"这是什么歌"、"帮我识歌"、"听歌识曲"、"识别一下这首歌"，或发送语音并询问歌曲信息。
        调用后会自动从消息上下文中的语音消息提取音频，通过 ACRCloud 识别歌曲，展示封面卡片并下载发送。
        '''
        logger.info("[MusicShare] LLM 语音识歌")
        async for result in self._handle_voice_recognition(event):
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
        raw = event.get_message_outline() or ""

        # ── Cache voice data per group whenever a voice message is sent ──
        gid = None
        if is_group_event(event):
            try:
                gid = str(event.get_group_id())
            except Exception:
                pass

        if gid and self.config_helper.voice_recognition_enabled():
            # Try CQ code first (OneBot v11)
            if "[CQ:record" in raw:
                url = self._parse_record_url_from_raw(raw)
                if url:
                    self._voice_cache[gid] = {"url": url, "ts": time.time()}
                    logger.info(f"[MusicShare] 缓存语音 URL (CQ) for group {gid}")
            else:
                # Try message chain Record (QQ Official / other platforms)
                record_comp = self._extract_record_from_chain(event)
                if record_comp:
                    try:
                        path = await record_comp.convert_to_file_path()
                        self._voice_cache[gid] = {"path": path, "ts": time.time()}
                        logger.info(f"[MusicShare] 缓存语音路径 (chain) for group {gid}: {path}")
                    except Exception as e:
                        logger.warning(f"[MusicShare] 缓存语音路径失败: {e}")

        # ── Voice recognition: @bot + voice/reply-to-voice only ──
        if self.config_helper.voice_recognition_enabled() and event.is_wake_up():
            has_record = "[CQ:record" in raw
            if not has_record:
                has_record = self._extract_record_from_chain(event) is not None
            has_reply_to_voice = (
                not has_record
                and self._extract_record_from_reply(event.message_obj) is not None
            )
            if has_record or has_reply_to_voice:
                logger.info(f"[MusicShare] 检测到@bot+语音，进入识曲")
                async for result in self._handle_voice_recognition(event):
                    yield result
                return

        result = extract_music_url(message_text)
        if result is None:
            return
        url, platform = result

        logger.info(f"[MusicShare] 检测到 {platform.name} 链接: {url}")

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

        if song_info.cover_url:
            async for result in self._send_cover_card(event, song_info):
                yield result

        async for result in self._download_then_send(
            event, song_info.title, song_info.artist,
            expected_duration=song_info.duration,
        ):
            yield result

    # ── Song info resolution ─────────────────────────────────────────────

    async def _resolve_song_info(self, url: str, platform: Platform) -> Optional[SongInfo]:
        """Resolve song metadata based on the platform type."""
        proxy = self.config_helper.proxy() or ""

        if platform == Platform.SPOTIFY:
            return await self.spotify_parser.parse(url)
        if platform == Platform.APPLE_MUSIC:
            return await self.apple_parser.parse(url)

        if platform in (Platform.YOUTUBE, Platform.SOUNDCLOUD, Platform.BILIBILI_AUDIO):
            return await self._resolve_via_ytdlp(url)

        if platform in (
            Platform.NETEASE, Platform.QQ_MUSIC,
            Platform.KUGOU, Platform.KUWO, Platform.MIGU,
            Platform.QISHUI,
        ):
            data = await parse_html_title(url, proxy)
            if data:
                cover_url = data.get("cover_url", "")
                # Domestic share pages often lack og:image → fallback to yt-dlp thumbnail
                if not cover_url and data["title"]:
                    try:
                        llm_info = await self._resolve_llm_song_info(
                            f"{data['title']} {data.get('artist', '')}"
                        )
                        if llm_info and llm_info.cover_url:
                            cover_url = llm_info.cover_url
                            logger.info(
                                f"[MusicShare] 用 yt-dlp 获取缩略图: {cover_url[:80]}"
                            )
                    except Exception:
                        pass
                return SongInfo(
                    title=data["title"],
                    artist=data.get("artist", ""),
                    cover_url=cover_url,
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
                python_exe, "-m", "yt_dlp", url,
                "--dump-json", "--no-warnings", "--skip-download",
                "--quiet", "--no-playlist",
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
                "--quiet", "--no-playlist", "--no-warnings",
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

    # ── LLM Smart Picker ──────────────────────────────────────────────────

    def _create_llm_picker(self, event: AstrMessageEvent):
        """Create a closure that uses LLM to pick the best candidate from search results."""
        from .downloader import Candidate

        async def llm_pick(candidates: list, title: str, artist: str):
            if not candidates:
                return None
            if len(candidates) == 1:
                return candidates[0]

            try:
                provider_id = self.config_helper.llm_search_provider()
                if not provider_id:
                    umo = event.unified_msg_origin
                    provider_id = await self.context.get_current_chat_provider_id(umo)

                cand_lines = []
                for i, c in enumerate(candidates):
                    dur_str = self._format_duration(c.duration) if c.duration else "?"
                    cand_lines.append(
                        f"  [{i}] {c.title} | 时长: {dur_str} | 来源: {c.source}"
                    )
                cand_text = "\n".join(cand_lines)

                prompt = f"""从以下候选歌曲中选出最匹配的一首。

目标歌曲: {title} - {artist}

候选列表:
{cand_text}

请只返回最匹配的候选编号（如 0, 1, 2...），不要返回其他内容。"""

                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                )
                answer = resp.completion_text.strip()
                m = _re.search(r"\d+", answer)
                if m:
                    idx = int(m.group())
                    if 0 <= idx < len(candidates):
                        logger.info(
                            f"[MusicShare] LLM picked candidate [{idx}]: "
                            f"{candidates[idx].title!r}"
                        )
                        return candidates[idx]

                logger.warning(
                    f"[MusicShare] LLM returned unparseable response, falling back to scoring: {answer[:100]}"
                )
                return None
            except Exception as e:
                logger.warning(f"[MusicShare] LLM picker failed, falling back to scoring: {e}")
                return None

        return llm_pick

    # ── Voice Recognition ─────────────────────────────────────────────────

    def _extract_record_from_chain(self, event: AstrMessageEvent):
        """Extract Record component from the event's message chain, if any."""
        try:
            message_chain = event.get_message_chain()
            for comp in message_chain:
                if comp.type == ComponentType.Record:
                    return comp
        except Exception:
            pass
        return None

    def _extract_record_from_reply(self, message_obj):
        """Extract Record component from a Reply message's chain."""
        if not message_obj:
            return None
        try:
            for comp in message_obj.message:
                if comp.type == ComponentType.Reply:
                    reply_chain = getattr(comp, 'chain', None) or []
                    for rc in reply_chain:
                        if rc.type == ComponentType.Record:
                            return rc
        except Exception:
            pass
        return None

    def _parse_record_url_from_raw(self, raw_msg: str) -> Optional[str]:
        """Extract the audio file URL from a CQ record code in raw message."""
        m = _re.search(r'\[CQ:record,[^\]]*url=([^,\]]+)', raw_msg)
        if m:
            return m.group(1).strip()
        m = _re.search(r'\[CQ:record,[^\]]*file=([^,\]]+)', raw_msg)
        if m:
            return m.group(1).strip()
        return None

    def _extract_reply_caption(self, message_obj) -> str:
        """Extract text description from a Reply context (LLM caption of the voice)."""
        if not message_obj:
            return ""
        try:
            for comp in message_obj.message:
                if comp.type == ComponentType.Reply:
                    reply_text = getattr(comp, 'message_str', '') or ''
                    if reply_text:
                        return reply_text.strip()
        except Exception:
            pass
        return ""

    def _extract_song_meta_from_event(self, event: AstrMessageEvent) -> tuple:
        """Extract song title/artist from QQ music share message metadata.
        
        QQ music voice messages carry 'source' (song name) and 'character' (artist) 
        fields in the raw data. Returns (title, artist) or ("", "").
        """
        try:
            # Try raw_message on the message_obj (QQ Official attaches metadata here)
            raw = getattr(event.message_obj, 'raw_message', None)
            if raw and isinstance(raw, dict):
                src = raw.get('source', '') or ''
                char = raw.get('character', '') or ''
                if src:
                    logger.info(f"[MusicShare] 从 raw_message 元数据提取: source={src}, character={char}")
                    return src.strip(), char.strip()
        except Exception:
            pass
        return "", ""

    @staticmethod
    def _parse_title_artist_from_text(text: str) -> tuple:
        """Parse title and artist from a free-form text like '光るなら - Goose house'."""
        text = text.strip()
        # Pattern: "Song - Artist" or "Song by Artist"
        for sep in (' - ', ' – ', ' — ', ' by ', ' / '):
            if sep in text:
                parts = text.rsplit(sep, 1)
                return parts[0].strip(), parts[1].strip()
        return text, ""

    def _get_cached_voice(self, event: AstrMessageEvent) -> Optional[str]:
        """Get cached voice URL or file path for this group (reply fallback)."""
        try:
            gid = str(event.get_group_id())
        except Exception:
            return None
        entry = self._voice_cache.get(gid)
        if entry and time.time() - entry["ts"] < self._voice_cache_ttl:
            return entry.get("url") or entry.get("path")
        return None

    async def _download_voice_url(self, url: str) -> Optional[str]:
        """Download voice from HTTPS URL to a local temp file."""
        temp_dir = Path(StarTools.get_data_dir("music_share")) / "voice_temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        local_path = str(temp_dir / f"voice_{uuid.uuid4().hex}.amr")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        with open(local_path, "wb") as f:
                            f.write(await resp.read())
                        return local_path
                    else:
                        logger.warning(f"[MusicShare] 语音下载失败 HTTP {resp.status}")
                        return None
        except Exception as e:
            logger.warning(f"[MusicShare] 语音下载异常: {e}")
            return None

    async def _handle_voice_recognition(self, event: AstrMessageEvent):
        """Handle voice recognition: download audio, recognize via ACRCloud, then download song."""
        if not self.config_helper.voice_recognition_enabled():
            return

        host = self.config_helper.acrcloud_host()
        access_key = self.config_helper.acrcloud_access_key()
        access_secret = self.config_helper.acrcloud_access_secret()

        if not host or not access_key or not access_secret:
            yield event.plain_result("语音识歌未配置 ACRCloud 密钥，请在插件设置中填写。免费注册见 README。")
            return

        raw = event.get_message_outline() or ""
        audio_path = None
        should_clean = False

        try:
            # 1) Direct voice message — Record in message_chain
            record = self._extract_record_from_chain(event)
            if record:
                audio_path = await record.convert_to_file_path()
                logger.info(f"[MusicShare] 语音文件已下载 (chain): {audio_path}")

            # 2) Current message contains [CQ:record — parse URL
            if not audio_path and "[CQ:record" in raw:
                url = self._parse_record_url_from_raw(raw)
                if url:
                    logger.info(f"[MusicShare] 下载语音 URL (raw): {url}")
                    audio_path = await self._download_voice_url(url)
                    should_clean = True

            # 3) Reply to a voice — extract Record from Reply.chain
            reply_caption = ""
            if not audio_path:
                record = self._extract_record_from_reply(event.message_obj)
                if record:
                    audio_path = await record.convert_to_file_path()
                    logger.info(f"[MusicShare] 语音文件已下载 (reply.chain): {audio_path}")
                # Also try to get caption from Reply context (LLM description of the voice)
                reply_caption = self._extract_reply_caption(event.message_obj)
                if reply_caption:
                    logger.info(f"[MusicShare] 从引用中提取到 caption: {reply_caption[:100]}")
                # Fallback: try cached URL or local path
                if not audio_path:
                    cached = self._get_cached_voice(event)
                    if cached:
                        if os.path.exists(cached):
                            audio_path = cached
                            logger.info(f"[MusicShare] 使用缓存语音路径: {audio_path}")
                        else:
                            logger.info(f"[MusicShare] 下载语音 URL (cached): {cached}")
                            audio_path = await self._download_voice_url(cached)
                            should_clean = True

            if not audio_path:
                yield event.plain_result("未在消息中找到语音。请直接发送语音消息并附上「识歌」「什么歌」等文字。")
                return

            title, artist = await self._acr_recognize(
                audio_path, host, access_key, access_secret
            )

            # ACRCloud fingerprint failed — try text-based fallback from reply caption
            if not title and reply_caption:
                title, artist = self._parse_title_artist_from_text(reply_caption)
                if title:
                    logger.info(
                        f"[MusicShare] ACRCloud 失败，使用 caption 文本匹配: "
                        f"title={title}, artist={artist}"
                    )
                    yield event.plain_result(
                        f"音频指纹未匹配，尝试根据描述搜索: {title} {artist}".strip()
                    )

            # ACRCloud + caption both failed — try QQ music metadata (source/character)
            if not title:
                title, artist = self._extract_song_meta_from_event(event)
                if title:
                    logger.info(
                        f"[MusicShare] ACRCloud 失败，使用 QQ 元数据: "
                        f"title={title}, artist={artist}"
                    )
                    yield event.plain_result(
                        f"根据消息元数据搜索: {title} {artist}".strip()
                    )

            if not title:
                yield event.plain_result("未识别到歌曲，请确认语音中包含清晰的原曲片段，而非哼唱。")
                return

            logger.info(f"[MusicShare] ACRCloud 识别成功: {title} - {artist}")
            yield event.plain_result(f"识别到歌曲: {title} - {artist}")

            info = await self._resolve_llm_song_info(f"{title} {artist}")
            if info and info.cover_url:
                async for result in self._send_cover_card(event, info):
                    yield result

            expected_dur = info.duration if info else ""
            async for result in self._download_then_send(
                event, title, artist, expected_duration=expected_dur,
            ):
                yield result

        except Exception as e:
            logger.error(f"[MusicShare] 语音识歌失败: {e}")
            yield event.plain_result(f"语音识歌失败: {e}。该平台可能不支持语音消息接收。")
        finally:
            if audio_path and should_clean:
                try:
                    Path(audio_path).unlink(missing_ok=True)
                except Exception:
                    pass

    async def _acr_recognize(self, audio_path: str, host: str, access_key: str, access_secret: str):
        """Recognize song from audio using ACRCloud. Returns (title, artist) or (None, None).
        
        Uses recognize_type=BOTH (audio fingerprint + humming) for best chance of matching
        compressed/low-quality audio like QQ voice messages.
        Retains original sample rate — ACRCloud SDK handles conversion internally.
        """
        try:
            from acrcloud.recognizer import ACRCloudRecognizer, ACRCloudRecognizeType
            import os as _os_local
            
            file_size = _os_local.path.getsize(audio_path)
            logger.info(
                f"[MusicShare] 准备 ACRCloud 识别: file={Path(audio_path).name}, "
                f"size={file_size} bytes"
            )

            config = {
                'host': host,
                'access_key': access_key,
                'access_secret': access_secret,
                'timeout': 10,
                # BOTH mode: tries audio fingerprint first, falls back to humming
                'recognize_type': ACRCloudRecognizeType.ACR_OPT_REC_BOTH,
            }

            def _recognize():
                recognizer = ACRCloudRecognizer(config)
                # rec_length=12: use up to 12 seconds of audio for better matching
                return recognizer.recognize_by_file(audio_path, 0, rec_length=12)

            result_str = await asyncio.to_thread(_recognize)
            result = json.loads(result_str)
            
            status_code = result.get("status", {}).get("code")
            status_msg = result.get("status", {}).get("msg", "")
            logger.info(
                f"[MusicShare] ACRCloud 识别结果: status={status_code}, "
                f"msg={status_msg}"
            )

            if status_code == 0:
                music_list = result.get("metadata", {}).get("music", [])
                if music_list:
                    music = music_list[0]
                    title = music.get("title", "")
                    artists = music.get("artists", [])
                    artist = artists[0].get("name", "") if artists else ""
                    score = music.get("score", 0)
                    logger.info(
                        f"[MusicShare] ACRCloud 命中: {title} - {artist} (score={score})"
                    )
                    return title, artist
                else:
                    logger.warning("[MusicShare] ACRCloud status=0 但无 music 结果")
            elif status_code == 1001:
                logger.warning("[MusicShare] ACRCloud 无匹配结果 (1001: No result)")
            elif status_code == 3003:
                logger.warning("[MusicShare] ACRCloud 配额用完 (3003)")
            else:
                logger.warning(f"[MusicShare] ACRCloud 返回非预期状态: code={status_code}, msg={status_msg}")

            return None, None
        except ImportError:
            logger.error("[MusicShare] pyacrcloud not installed. Run: pip install pyacrcloud")
            return None, None
        except Exception as e:
            logger.error(f"[MusicShare] ACRCloud recognition error: {e}")
            return None, None

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _send_cover_card(self, event: AstrMessageEvent, song_info: SongInfo):
        """Generate and send the info card as an image."""
        try:
            proxy = self.config_helper.proxy() or ""
            card = await make_info_card(
                song_info.cover_url, song_info.title, song_info.artist,
                song_info.album, song_info.source, proxy,
                duration=song_info.duration, release_date=song_info.release_date,
            )
            temp_dir = Path(self.config_helper.download_dir() or "data/music")
            temp_dir.mkdir(parents=True, exist_ok=True)
            safe_name = song_info.title[:20].replace("/", "_")
            card_path = temp_dir / f"cover_{safe_name}.png"
            card.save(str(card_path), "PNG")
            yield event.set_result(event.chain_result([Image.fromFileSystem(str(card_path))]))
            card_path.unlink()
        except Exception as e:
            logger.error(f"[MusicShare] 封面卡片生成失败: {e}")
            yield event.plain_result(
                f"歌名: {song_info.title}\n艺术家: {song_info.artist}\n来源: {song_info.source}"
            )

    async def _download_then_send(
        self, event: AstrMessageEvent, title: str, artist: str,
        expected_duration: str = "", force_mode: str = None,
    ):
        """Download audio and send voice + file."""
        download_dir = Path(StarTools.get_data_dir("music_share")) / "downloads"
        download_dir.mkdir(parents=True, exist_ok=True)

        llm_picker = None
        if self.config_helper.llm_search_enabled():
            llm_picker = self._create_llm_picker(event)

        audio_file, error_msg = await self.downloader.search_and_download(
            title, artist,
            expected_duration=expected_duration,
            match_threshold=self.config_helper.match_threshold(),
            download_dir=download_dir,
            llm_picker=llm_picker,
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
            mode = force_mode or self.config_helper.send_mode()
            record_sent = False

            if mode in ("仅语音", "都发送"):
                try:
                    record = Record.fromFileSystem(str(audio_file))
                    yield event.set_result(event.chain_result([record]))
                    record_sent = True
                except Exception as e:
                    logger.warning(f"[MusicShare] Record 发送失败: {e}")
                    if mode == "仅语音":
                        logger.info("[MusicShare] 回退为发送文件")
                        try:
                            file_b64 = file_to_base64(str(audio_file))
                            yield event.set_result(event.chain_result([File(name=audio_file.name, url=file_b64)]))
                            record_sent = True
                        except Exception as fe:
                            logger.error(f"[MusicShare] 文件发送也失败: {fe}")
                            yield event.plain_result(f"发送失败: {fe}")
                    else:
                        logger.info("[MusicShare] Record 失败但仍会尝试发送 File")

            if mode in ("仅文件", "都发送"):
                try:
                    file_b64 = file_to_base64(str(audio_file))
                    yield event.set_result(event.chain_result([File(name=audio_file.name, url=file_b64)]))
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