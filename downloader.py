"""Music search & download wrapper (yt-dlp)."""
import asyncio, json, re
from dataclasses import dataclass
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

        candidates = await self._search(query)

        if not candidates:
            return None, f"未搜到匹配结果: {query}"

        if llm_picker is not None:
            best = await llm_picker(candidates, title, artist)
            if best is None:
                best = self._pick_best(candidates, expected_secs, match_threshold, title)
            else:
                logger.info(
                    f"[MusicShare] LLM Selected: {best.title!r} "
                    f"({best.source}, {best.duration}s)"
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

    async def _search(self, query: str) -> List[Candidate]:
        """Search YouTube via yt-dlp ytsearch3.

        Uses --ignore-no-formats-error to skip individual results that have
        no available formats (deleted/restricted videos).  Even if yt-dlp
        exits non-zero, we parse whatever JSON lines were successfully output.
        """
        python_exe = self._find_python()
        if not python_exe: return []

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
            candidates = []
            output = stdout.decode("utf-8", errors="replace").strip()
            for line in output.split("\n"):
                if not line: continue
                try:
                    d = json.loads(line)
                    dur = float(d.get("duration", 0) or 0)
                    if dur <= 0:
                        continue
                    candidates.append(Candidate(
                        duration=dur,
                        title=d.get("title", ""),
                        source="yt-dlp", yt_id=d.get("id", "")))
                except Exception:
                    continue

            if candidates:
                return candidates

            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")[:300]
                logger.warning(f"[MusicShare] yt-dlp search error: {err[:100]}")
            return []
        except asyncio.TimeoutError:
            logger.warning("[MusicShare] yt-dlp search timeout")
            return []
        except Exception as e:
            logger.warning(f"[MusicShare] yt-dlp search exception: {e}")
            return []

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
            neg_penalty = 0.0
            if (not cls._NEGATIVE_KEYWORDS.search(clean_orig) and
                    cls._NEGATIVE_KEYWORDS.search(cand_title)):
                neg_penalty = 0.5
            total = 0.5 * dur_score + 0.5 * title_score - neg_penalty
            if total > best_score: best_score, best = total, cand
        return best

    async def _download(
        self, cand: Candidate, download_dir: Path,
        original_title: str = "", original_artist: str = "",
    ) -> Tuple[Optional[Path], str]:
        """Download audio for a candidate.

        1. Try direct YouTube ID download
        2. Fallback: ytsearch with original title+artist
        """
        output_template = str(download_dir / "%(title)s.%(ext)s")
        python_exe = self._find_python()
        if not python_exe: return None, "未找到 Python 解释器"

        # Try direct YouTube ID download first
        if cand.yt_id:
            query = f"https://www.youtube.com/watch?v={cand.yt_id}"
            filepath, err = await self._ytdlp_download(query, output_template)
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
        filepath, err = await self._ytdlp_download(query, output_template)
        if filepath:
            return filepath, ""
        return None, err or "下载失败"

    async def _ytdlp_download(
        self, query: str, output_template: str,
    ) -> Tuple[Optional[Path], str]:
        """Download via yt-dlp.  Uses -x (extract audio) without forcing
        a specific codec, letting yt-dlp pick the best available format."""
        python_exe = self._find_python()
        if not python_exe: return None, "未找到 Python 解释器"

        cmd = [python_exe, "-m", "yt_dlp", query,
               "--no-playlist", "--no-warnings",
               "-x",  # extract audio (let yt-dlp choose best codec)
               f"--audio-quality={self.config.audio_quality()}K",
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
            return None, "下载超时，请检查网络/代理"
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