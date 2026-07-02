"""Dual-engine music search & download wrapper (yt-dlp + spotdl)."""
import asyncio, json, os, re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Tuple
from rapidfuzz import fuzz as rfuzz
from astrbot.api import logger
from .config import ConfigHelper

@dataclass
class Candidate:
    duration: float
    title: str
    source: str
    yt_id: str = ""
    cover_url: str = ""
    spotify_url: str = ""

class MusicDownloader:
    def __init__(self, config: ConfigHelper):
        self.config = config

    async def search_and_download(
        self, title: str, artist: str,
        expected_duration: str, match_threshold: int, download_dir: Path,
        llm_picker=None,
    ) -> Tuple[Optional[Path], str]:
        query = f"{title} {artist}".strip()
        query = re.sub(r'[<>|"&!$`]', "", query).strip()
        if not query:
            return None, "搜索关键词为空"

        download_dir.mkdir(parents=True, exist_ok=True)
        expected_secs = self._parse_duration(expected_duration)

        candidates, errors = await self._search_meta(query)

        if not candidates:
            if errors:
                err_detail = "; ".join(errors)
                return None, f"搜索失败: {err_detail}"
            return None, f"未搜到匹配结果: {query}"

        if llm_picker is not None:
            best = await llm_picker(candidates, title, artist)
            if best is None:
                best = self._pick_best(candidates, expected_secs, match_threshold, title)
            else:
                logger.info(
                    f"[MusicShare] LLM Selected: {best.title!r} ({best.source}, "
                    f"{best.duration}s)"
                )
        else:
            best = self._pick_best(candidates, expected_secs, match_threshold, title)

        if best is None:
            return None, (f"未找到时长匹配的歌曲 (精度 {match_threshold}%, "
                         f"预期 {expected_secs}s)")
        if llm_picker is None:
            logger.info(
                f"[MusicShare] Selected: {best.title!r} ({best.source}, "
                f"{best.duration}s vs expected {expected_secs}s)"
            )
        filepath, dl_err = await self._download(best, download_dir, title, artist)
        if not filepath:
            return None, dl_err or "下载失败"
        return filepath, ""

    async def _search_meta(self, query: str) -> Tuple[List[Candidate], List[str]]:
        """双引擎竞速搜索：yt-dlp 必须完成，spotdl 作为补充最多额外等 10 秒。

        yt-dlp 先返回 → 单引擎候选用；spotdl 在 10 秒内也返回 → 合并提高匹配精度。
        spotdl 超时或失败不阻塞整体流程。
        """
        SPOTDL_GRACE_SECS = 10

        candidates: List[Candidate] = []
        errors: List[str] = []

        ytdlp_task = asyncio.create_task(self._ytdlp_search(query))
        spotdl_task = asyncio.create_task(self._spotdl_search(query))

        # 1) 等 yt-dlp（使用用户配置的 search_timeout，必须完成）
        try:
            ytdlp_result = await ytdlp_task
            if ytdlp_result:
                candidates.extend(ytdlp_result)
        except Exception as e:
            errors.append(f"yt-dlp: {e}")
            logger.warning(f"[MusicShare] yt-dlp search error: {e}")

        # 2) yt-dlp 完成后，spotdl 可能：已完成 / 还在跑 / 已抛异常
        if spotdl_task.done():
            try:
                spotdl_result = spotdl_task.result()
                if spotdl_result:
                    candidates.extend(spotdl_result)
            except Exception as e:
                errors.append(f"spotdl: {e}")
                logger.warning(f"[MusicShare] spotdl search error: {e}")
        else:
            try:
                spotdl_result = await asyncio.wait_for(
                    spotdl_task, timeout=SPOTDL_GRACE_SECS,
                )
                if spotdl_result:
                    candidates.extend(spotdl_result)
            except asyncio.TimeoutError:
                spotdl_task.cancel()
                try:
                    await spotdl_task
                except (asyncio.CancelledError, Exception):
                    pass
                logger.info(
                    f"[MusicShare] spotdl 未在 {SPOTDL_GRACE_SECS}s 内返回，"
                    f"仅使用 yt-dlp 结果"
                )
            except Exception as e:
                errors.append(f"spotdl: {e}")
                logger.warning(f"[MusicShare] spotdl search error: {e}")

        return candidates, errors

    async def _ytdlp_search(self, query: str) -> Optional[List[Candidate]]:
        """Search YouTube via yt-dlp ytsearch.

        Uses --ignore-no-formats-error to skip individual results that have
        no available formats (deleted/restricted videos).  Even if yt-dlp
        exits non-zero, we parse whatever JSON lines were successfully output.
        """
        python_exe = self._find_python()
        if not python_exe: return None
        cmd = [python_exe, "-m", "yt_dlp", f"ytsearch3:{query}",
               "--dump-json", "--no-warnings", "--no-playlist",
               "--skip-download", "--quiet",
               "--ignore-no-formats-error"]
        proxy = self.config.proxy()
        if proxy: cmd.extend(["--proxy", proxy])
        try:
            proc = await asyncio.create_subprocess_exec(*cmd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.config.search_timeout())
            # Parse any JSON lines that were successfully output, even on error
            candidates = []
            output = stdout.decode("utf-8", errors="replace").strip()
            for line in output.split("\n"):
                if not line: continue
                try:
                    d = json.loads(line)
                    dur = float(d.get("duration", 0) or 0)
                    if dur <= 0:
                        continue  # skip videos with no duration (live, deleted)
                    candidates.append(Candidate(
                        duration=dur,
                        title=d.get("title", ""),
                        source="yt-dlp", yt_id=d.get("id", "")))
                except Exception:
                    continue

            if candidates:
                return candidates

            # No usable candidates – check if there was a hard error
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")[:300]
                if "bot" in err.lower() or "login" in err.lower():
                    raise RuntimeError("YouTube 反爬拦截，请检查代理/网络")
                if "timed out" in err.lower():
                    raise asyncio.TimeoutError("搜索超时")
                raise RuntimeError(f"yt-dlp 搜索失败: {err[:100]}")
            return None
        except asyncio.TimeoutError:
            raise RuntimeError("YouTube 搜索超时，请检查网络或代理")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"yt-dlp 搜索异常: {e}")

    async def _spotdl_search(self, query: str) -> Optional[List[Candidate]]:
        """Search Spotify via spotdl save (subprocess).  Returns candidates with
        Spotify metadata and YouTube IDs for download.

        If spotdl times out or fails, this is handled gracefully by the race
        logic in _search_meta – yt-dlp results are used instead.
        """
        client_id = self.config.spotify_client_id()
        client_secret = self.config.spotify_client_secret()
        if not client_id or not client_secret:
            return None  # Silently skip – no credentials

        python_exe = self._find_python()
        if not python_exe: return None

        self._ensure_spotdl_config(client_id, client_secret)

        cmd = [
            python_exe, "-m", "spotdl", "save", query, "--save-file", "-",
            "--headless",
            "--client-id", client_id,
            "--client-secret", client_secret,
        ]
        proxy = self.config.proxy()
        if proxy: cmd.extend(["--proxy", proxy])
        try:
            proc = await asyncio.create_subprocess_exec(*cmd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.config.search_timeout())
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")[:200]
                raise RuntimeError(f"spotdl 搜索失败: {err[:80]}")
            candidates = []
            for line in stdout.decode("utf-8", errors="replace").strip().split("\n"):
                if not line: continue
                try:
                    d = json.loads(line)
                    candidates.append(Candidate(
                        duration=float(d.get("duration", 0) or 0),
                        title=d.get("name", "") or d.get("title", ""),
                        source="spotdl",
                        yt_id=d.get("yt_id", "") or d.get("youtube_id", ""),
                        spotify_url=d.get("url", "") or d.get("spotify_url", ""),
                    ))
                except Exception:
                    continue
            return candidates or None
        except asyncio.TimeoutError:
            raise RuntimeError("Spotify 搜索超时，请检查网络或代理")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"spotdl 搜索异常: {e}")

    @staticmethod
    def _ensure_spotdl_config(client_id: str, client_secret: str):
        """Write ~/.spotdl/config.json with Spotify OAuth credentials."""
        import os as _os_module, json as _json_module
        _os_module.makedirs(_os_module.path.expanduser("~/.spotdl"), exist_ok=True)
        _config_path = _os_module.path.expanduser("~/.spotdl/config.json")
        _config = {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_token": None,
            "user_auth": False,
            "headless": True,
            "cache_path": _os_module.path.expanduser("~/.spotdl/.spotipy"),
            "no_cache": False,
            "max_retries": 3,
            "use_cache_file": False,
        }
        with open(_config_path, "w") as f:
            _json_module.dump(_config, f)
        logger.debug(f"[MusicShare] spotdl config written to {_config_path}")

    @staticmethod
    def _parse_duration(dur: str) -> float:
        if not dur: return 0.0
        dur = dur.strip()
        try: return float(dur)
        except ValueError: pass
        parts = dur.split(":")
        if len(parts) == 3: return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2: return int(parts[0]) * 60 + float(parts[1])
        return 0.0

    _NEGATIVE_KEYWORDS = re.compile(
        r"\b(instrumental|karaoke|piano|cover|remix|tribute|acoustic|"
        r"live|orchestral|backing\s*track|concert|unplugged|mix|edit|"
        r"orchestra|quartet|trio|band\s*version|rehearsal|demo|nightcore|"
        r"slowed|sped\s*up|vocal\s*only|off\s*vocal|minus\s*one)\b", re.IGNORECASE)

    @classmethod
    def _pick_best(cls, candidates, expected_secs, threshold_pct, original_title):
        if not candidates: return None
        has_duration = expected_secs > 0
        max_deviation = (100 - threshold_pct) / 100.0
        clean_orig = original_title.lower().strip()
        best, best_score = None, -999.0
        for cand in candidates:
            if has_duration:
                deviation = abs(cand.duration - expected_secs) / expected_secs
                if deviation > max_deviation: continue
                dur_score = 1.0 - deviation
            else:
                dur_score = 0.5
            cand_title = cand.title.lower().strip()
            title_score = rfuzz.partial_ratio(clean_orig, cand_title) / 100.0
            source_score = 0.8 if cand.source == "spotdl" else 0.2
            neg_penalty = 0.0
            if (not cls._NEGATIVE_KEYWORDS.search(clean_orig) and
                    cls._NEGATIVE_KEYWORDS.search(cand_title)):
                neg_penalty = 0.5
            total = 0.4 * dur_score + 0.4 * title_score + 0.2 * source_score - neg_penalty
            if total > best_score: best_score, best = total, cand
        return best

    async def _download(
        self, cand: Candidate, download_dir: Path,
        original_title: str = "", original_artist: str = "",
    ) -> Tuple[Optional[Path], str]:
        """Download audio for a candidate.

        - spotdl candidates with spotify_url: use spotdl download (best accuracy)
        - spotdl/yt-dlp candidates with yt_id: use yt-dlp
        - Fallback: ytsearch with original_title + original_artist
        """
        output_template = str(download_dir / "%(title)s.%(ext)s")
        python_exe = self._find_python()
        if not python_exe: return None, "未找到 Python 解释器"

        # spotdl candidate with Spotify URL → use spotdl for precise download
        if cand.source == "spotdl" and cand.spotify_url:
            return await self._download_via_spotdl(
                cand.spotify_url, output_template, download_dir,
                original_title, original_artist,
            )

        # Try direct YouTube ID download first
        if cand.yt_id:
            if cand.source == "spotdl":
                query = f"https://music.youtube.com/watch?v={cand.yt_id}"
            else:
                query = f"https://www.youtube.com/watch?v={cand.yt_id}"

            filepath, err = await self._download_via_ytdlp(query, output_template)
            if filepath:
                return filepath, ""

            logger.info(
                f"[MusicShare] ID download failed ({err[:60]}), falling back to search"
            )

        # Fallback: search by original title+artist (not candidate title)
        fallback_query = f"{original_title} {original_artist}".strip()
        if not fallback_query:
            fallback_query = cand.title
        fallback_query = re.sub(r'[<>|"&!$`]', "", fallback_query).strip()

        logger.info(
            f"[MusicShare] Retrying with ytsearch3: {fallback_query[:60]}"
        )
        query = f"ytsearch3:{fallback_query}"
        filepath, err = await self._download_via_ytdlp(query, output_template)
        if filepath:
            return filepath, ""
        return None, err or "下载失败"

    async def _download_via_spotdl(
        self, spotify_url: str, output_template: str, download_dir: Path,
        original_title: str = "", original_artist: str = "",
    ) -> Tuple[Optional[Path], str]:
        """Download via spotdl from a Spotify URL.  Falls back to yt-dlp on failure."""
        python_exe = self._find_python()
        if not python_exe: return None, "未找到 Python 解释器"

        client_id = self.config.spotify_client_id()
        client_secret = self.config.spotify_client_secret()

        cmd = [
            python_exe, "-m", "spotdl", "download", spotify_url,
            "--output", str(download_dir),
            "--headless",
            "--client-id", client_id or "",
            "--client-secret", client_secret or "",
            f"--format={self.config.audio_format()}",
            f"--bitrate={self.config.audio_quality()}k",
        ]
        proxy = self.config.proxy()
        if proxy:
            cmd.extend(["--proxy", proxy])

        try:
            proc = await asyncio.create_subprocess_exec(*cmd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.config.search_timeout() * 3,
            )
            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace")[:200]

            if proc.returncode == 0 and stdout_text:
                fp = Path(stdout_text.split("\n")[-1].strip())
                if fp.exists():
                    return fp, ""

            # Try scanning download_dir for the newest audio file
            try:
                newest = max(
                    download_dir.glob(f"*.{self.config.audio_format()}"),
                    key=lambda p: p.stat().st_mtime, default=None,
                )
                if newest:
                    return newest, ""
            except Exception:
                pass

            # spotdl failed – fallback to yt-dlp
            if stderr_text:
                logger.warning(
                    f"[MusicShare] spotdl download failed, fallback yt-dlp: {stderr_text[:100]}"
                )
            fallback = self._fallback_query(original_title, original_artist)
            return await self._download_via_ytdlp(f"ytsearch3:{fallback}", output_template)

        except asyncio.TimeoutError:
            logger.warning("[MusicShare] spotdl download timeout, fallback yt-dlp")
            fallback = self._fallback_query(original_title, original_artist)
            return await self._download_via_ytdlp(f"ytsearch3:{fallback}", output_template)
        except Exception as e:
            logger.warning(f"[MusicShare] spotdl download error: {e}, fallback yt-dlp")
            fallback = self._fallback_query(original_title, original_artist)
            return await self._download_via_ytdlp(f"ytsearch3:{fallback}", output_template)

    async def _download_via_ytdlp(
        self, query: str, output_template: str,
    ) -> Tuple[Optional[Path], str]:
        """Download via yt-dlp."""
        python_exe = self._find_python()
        if not python_exe: return None, "未找到 Python 解释器"

        cmd = [python_exe, "-m", "yt_dlp", query,
               "--no-playlist", "--no-warnings", "--extract-audio",
               f"--audio-format={self.config.audio_format()}",
               f"--audio-quality={self.config.audio_quality()}",
               "--max-filesize", f"{self.config.max_file_size_mb()}M",
               "--output", output_template, "--no-progress",
               "--ignore-no-formats-error",
               "--print", "after_move:filepath"]
        proxy = self.config.proxy()
        if proxy: cmd.extend(["--proxy", proxy])
        try:
            proc = await asyncio.create_subprocess_exec(*cmd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.config.search_timeout() * 2)
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")[:200]
                if "bot" in err.lower() or "login" in err.lower():
                    return None, "下载失败: YouTube 反爬拦截，请检查代理"
                return None, f"下载失败: {err[:80]}"
            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            if stdout_text:
                fp = Path(stdout_text.split("\n")[-1].strip())
                if fp.exists(): return fp, ""
            return None, "下载完成但未找到文件"
        except asyncio.TimeoutError:
            return None, "下载超时，请检查网络"
        except Exception as e:
            return None, f"下载异常: {e}"

    @staticmethod
    def _fallback_query(title: str, artist: str) -> str:
        """Build a clean search query from original title and artist."""
        query = f"{title} {artist}".strip()
        query = re.sub(r'[<>|"&!$`]', "", query).strip()
        return query or "unknown"

    @staticmethod
    def _find_python() -> Optional[str]:
        import sys
        try: return sys.executable
        except: return None

    @staticmethod
    def clean_file(fp: Path) -> None:
        try:
            if fp.exists(): fp.unlink()
        except: pass