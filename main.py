"""
astrbot_plugin_anymusic - AnyMusic 音乐插件

自动识别群聊中的主流音乐分享链接，解析元数据并双引擎竞争下载。
"""

import asyncio
import json
import re as _re
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import (
    ComponentType,
    File,
    Image,
    Record,
    file_to_base64,
)
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

        # ── Voice recognition fallback: keyword detection (with or without Record) ──
        if self.config_helper.voice_recognition_enabled():
            raw = event.get_message_outline() or ""
            has_record = "[CQ:record" in raw
            voice_keywords = ["识歌", "什么歌", "识别", "听歌识曲", "识曲"]
            if has_record or any(kw in message_text for kw in voice_keywords):
                logger.info("[MusicShare] 检测到语音识歌触发条件")
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

    def _parse_record_url_from_raw(self, raw_msg: str) -> Optional[str]:
        """Extract the audio file URL from a CQ record code in raw message."""
        m = _re.search(r'\[CQ:record,[^\]]*url=([^,\]]+)', raw_msg)
        if m:
            return m.group(1).strip()
        # Try file path fallback
        m = _re.search(r'\[CQ:record,[^\]]*file=([^,\]]+)', raw_msg)
        if m:
            return m.group(1).strip()
        return None

    def _extract_record_from_reply(self, message_obj) -> Optional[Record]:
        """Extract Record component from Reply.chain in a replied-to message."""
        if message_obj is None:
            return None
        for comp in message_obj.message:
            if comp.type == ComponentType.Reply:
                chain = getattr(comp, 'chain', None) or []
                for c in chain:
                    if c.type == ComponentType.Record:
                        return c
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

        # Try to get Record from message_chain (works on most platforms)
        record = None
        try:
            message_chain = event.get_message_chain()
            for comp in message_chain:
                if comp.type == ComponentType.Record:
                    record = comp
                    break
        except Exception:
            pass

        # Fallback: parse Record from raw outline
        if not record:
            raw = event.get_message_outline() or ""
            if "[CQ:record" in raw:
                url = self._parse_record_url_from_raw(raw)
                if url:
                    record = Record(file=url)

        # Fallback 2: reply to a voice message — extract Record from Reply.chain
        if not record:
            raw = event.get_message_outline() or ""
            is_reply = "[CQ:reply" in raw
            if is_reply:
                record = self._extract_record_from_reply(event.message_obj)
                if not record:
                    # Last resort: try AstrBot's auto-converted WAV
                    wav_found = await self._find_latest_temp_wav()
                    if wav_found:
                        logger.info(f"[MusicShare] 使用 AstrBot 临时 WAV: {wav_found}")
                        audio_path = wav_found
                        title, artist = await self._acr_recognize(
                            audio_path, host, access_key, access_secret
                        )
                        if not title:
                            yield event.plain_result("未识别到歌曲，请确认引用的语音中包含清晰的原曲片段。")
                            return
                        logger.info(f"[MusicShare] ACRCloud 识别成功: {title} - {artist}")
                        yield event.plain_result(f"识别到歌曲: {title} - {artist}")
                        info = await self._resolve_llm_song_info(f"{title} {artist}")
                        if info and info.cover_url:
                            async for result in self._send_cover_card(event, info):
                                yield result
                        expected_dur = info.duration if info else ""
                        async for result in self._download_then_send(
                            event, title, artist,
                            expected_duration=expected_dur,
                        ):
                            yield result
                        return

        if not record:
            yield event.plain_result(
                "未在消息中找到语音。请直接发送语音消息（而非引用回复），并附上「识歌」「什么歌」等文字。"
            )
            return

        audio_path = None
        wav_path = None
        try:
            audio_path = await record.convert_to_file_path()
            logger.info(f"[MusicShare] 语音文件已下载: {audio_path}")

            # QQ 语音是 AMR/SILK 格式，需用 ffmpeg 转 WAV
            wav_path = await self._convert_to_wav(audio_path)
            if wav_path:
                logger.info(f"[MusicShare] 音频已转码为 WAV: {wav_path}")
            else:
                wav_path = audio_path  # 转码失败则用原文件

            title, artist = await self._acr_recognize(wav_path, host, access_key, access_secret)

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
                event, title, artist,
                expected_duration=expected_dur,
            ):
                yield result

        except Exception as e:
            logger.error(f"[MusicShare] 语音识歌失败: {e}")
            yield event.plain_result(f"语音识歌失败: {e}。该平台可能不支持语音消息接收。")
        finally:
            if audio_path:
                try:
                    Path(audio_path).unlink(missing_ok=True)
                except Exception:
                    pass

    async def _find_latest_temp_wav(self) -> Optional[str]:
        """Find the most recently created media_audio_*.wav in AstrBot's temp directory.
        
        AstrBot auto-converts voice messages to WAV at /AstrBot/data/temp/media_audio_*.wav
        """
        temp_dir = Path("/AstrBot/data/temp")
        if not temp_dir.exists():
            return None
        wavs = sorted(
            temp_dir.glob("media_audio_*.wav"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return str(wavs[0]) if wavs else None

    async def _convert_to_wav(self, audio_path: str) -> Optional[str]:
        """Convert AMR/SILK audio to WAV using ffmpeg."""
        import uuid

        wav_path = str(Path(audio_path).with_suffix(".wav"))
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", audio_path,
                "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                wav_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=15,
            )
            if proc.returncode == 0 and Path(wav_path).exists():
                return wav_path
            logger.warning(
                f"[MusicShare] ffmpeg convert failed: {stderr.decode('utf-8', errors='replace')[:200]}"
            )
            return None
        except Exception as e:
            logger.warning(f"[MusicShare] ffmpeg not available or failed: {e}")
            return None

    async def _acr_recognize(self, audio_path: str, host: str, access_key: str, access_secret: str):
        """Recognize song from audio using ACRCloud. Returns (title, artist) or (None, None)."""
        try:
            from acrcloud.recognizer import ACRCloudRecognizer

            config = {
                'host': host,
                'access_key': access_key,
                'access_secret': access_secret,
                'timeout': 10,
            }

            def _recognize():
                recognizer = ACRCloudRecognizer(config)
                result_str = recognizer.recognize_by_file(audio_path, 0)
                return json.loads(result_str)

            result = await asyncio.to_thread(_recognize)

            if result.get("status", {}).get("code") == 0:
                music_list = result.get("metadata", {}).get("music", [])
                if music_list:
                    music = music_list[0]
                    title = music.get("title", "")
                    artists = music.get("artists", [])
                    artist = artists[0].get("name", "") if artists else ""
                    return title, artist

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
        force_mode: str = None,
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
            if force_mode:
                mode = force_mode
            else:
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