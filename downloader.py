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
            # LLM smart selection: let LLM pick the best match from candidates
            best = await llm_picker(candidates, title, artist)
            if best is None:
                # LLM failed, fall back to scoring
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
        filepath, dl_err = await self._download(best, download_dir)
        if not filepath:
            return None, dl_err or "下载失败"
        return filepath, ""

    async def _search_meta(self, query: str) -> Tuple[List[Candidate], List[str]]:
        results = await asyncio.gather(
            self._ytdlp_search(query), self._spotdl_search(query),
            return_exceptions=True,
        )
        candidates: List[Candidate] = []
        errors: List[str] = []
        for i, r in enumerate(results):
            engine = "yt-dlp" if i == 0 else "spotdl"
            if isinstance(r, Exception):
                errors.append(f"{engine}: {r}")
                logger.warning(f"[MusicShare] {engine} search error: {r}")
            elif r is not None:
                candidates.extend(r)
        return candidates, errors

    async def _ytdlp_search(self, query: str) -> Optional[List[Candidate]]:
        python_exe = self._find_python()
        if not python_exe: return None
        cmd = [python_exe, "-m", "yt_dlp", f"ytsearch3:{query}",
               "--dump-json", "--no-warnings", "--no-playlist",
               "--skip-download", "--quiet"]
        proxy = self.config.proxy()
        if proxy: cmd.extend(["--proxy", proxy])
        try:
            proc = await asyncio.create_subprocess_exec(*cmd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.config.search_timeout())
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")[:300]
                if "bot" in err.lower() or "login" in err.lower():
                    raise RuntimeError("YouTube 反爬拦截，请检查代理/网络")
                if "timed out" in err.lower():
                    raise asyncio.TimeoutError("搜索超时")
                raise RuntimeError(f"yt-dlp 搜索失败: {err[:100]}")
            candidates = []
            for line in stdout.decode("utf-8", errors="replace").strip().split("\n"):
                if not line: continue
                try:
                    d = json.loads(line)
                    candidates.append(Candidate(
                        duration=float(d.get("duration", 0) or 0),
                        title=d.get("title", ""),
                        source="yt-dlp", yt_id=d.get("id", "")))
                except: continue
            return candidates or None
        except asyncio.TimeoutError:
            raise RuntimeError("YouTube 搜索超时，请检查网络或代理")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"yt-dlp 搜索异常: {e}")

    async def _spotdl_search(self, query: str) -> Optional[List[Candidate]]:
        # spotdl 4.5.0 在 Docker 无浏览器环境下无法完成 OAuth 认证，
        # 即使配置了 Client ID/Secret 和 --headless 参数也会卡在认证流程直到超时。
        # 暂时禁用，仅依赖 yt-dlp 单引擎搜索。
        # 待上游修复后取消注释下面三行即可恢复双引擎。
        return None
        client_id = self.config.spotify_client_id()
        client_secret = self.config.spotify_client_secret()
        if not client_id or not client_secret:
            raise RuntimeError("spotdl 未配置 Spotify 凭据，跳过（可在插件设置中填写免费凭据）")

        python_exe = self._find_python()
        if not python_exe: return None

        # Write Spotify credentials to spotdl config
        self._ensure_spotdl_config(client_id, client_secret)

        cmd = [python_exe, "-m", "spotdl", "save", query, "--save-file", "-"]
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
                        yt_id=d.get("yt_id", "") or d.get("youtube_id", "")))
                except: continue
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

    async def _download(self, cand: Candidate, download_dir: Path) -> Tuple[Optional[Path], str]:
        output_template = str(download_dir / "%(title)s.%(ext)s")
        python_exe = self._find_python()
        if not python_exe: return None, "未找到 Python 解释器"
        if cand.source == "spotdl" and cand.yt_id:
            query = f"https://music.youtube.com/watch?v={cand.yt_id}"
        elif cand.source == "yt-dlp" and cand.yt_id:
            query = f"https://www.youtube.com/watch?v={cand.yt_id}"
        else:
            return None, f"无法获取 {cand.title!r} 的下载链接"
        cmd = [python_exe, "-m", "yt_dlp", query,
               "--no-playlist", "--no-warnings", "--extract-audio",
               f"--audio-format={self.config.audio_format()}",
               f"--audio-quality={self.config.audio_quality()}",
               "--max-filesize", f"{self.config.max_file_size_mb()}M",
               "--output", output_template, "--no-progress",
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
    def _find_python() -> Optional[str]:
        import sys
        try: return sys.executable
        except: return None

    @staticmethod
    def clean_file(fp: Path) -> None:
        try:
            if fp.exists(): fp.unlink()
        except: pass