"""Configuration helper for astrbot_plugin_music_share."""

from pathlib import Path


class ConfigHelper:
    """Typed accessor for plugin configuration."""

    def __init__(self, config):
        self.config = config

    def _cfg(self, key: str, default=None):
        if not self.config:
            return default
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    # ---- proxy ----
    def proxy(self) -> str:
        return str(self._cfg("proxy", "") or "").strip()

    # ---- download ----
    def download_dir(self) -> str:
        return str(self._cfg("download_dir", "data/music"))

    def audio_format(self) -> str:
        return str(self._cfg("audio_format", "mp3"))

    def audio_quality(self) -> int:
        return int(self._cfg("audio_quality", 192))

    def max_file_size_mb(self) -> int:
        return int(self._cfg("max_file_size_mb", 50))

    def search_timeout(self) -> int:
        return int(self._cfg("search_timeout", 30))

    def match_threshold(self) -> int:
        return int(self._cfg("match_threshold", 97))

    # ---- send mode ----
    def llm_tool_mode(self) -> str:
        return str(self._cfg("llm_tool_mode", "图片+语音")).strip()

    def send_mode(self) -> str:
        return str(self._cfg("send_mode", "都发送")).strip()

    # ---- groups ----
    def enabled_groups(self) -> list[str]:
        groups = self._cfg("enabled_groups", [])
        if not groups:
            return []
        return [str(g) for g in groups]

    def is_group_enabled(self, group_id: str) -> bool:
        enabled = self.enabled_groups()
        if not enabled:
            return True
        return group_id in enabled

    def llm_search_enabled(self) -> bool:
        return bool(self._cfg("llm_search_enabled", False))

    def llm_search_provider(self) -> str:
        return str(self._cfg("llm_search_provider", "") or "").strip()

    def voice_recognition_enabled(self) -> bool:
        return bool(self._cfg("enable_voice_recognition", False))

    def acrcloud_host(self) -> str:
        return str(self._cfg("acrcloud_host", "") or "").strip()

    def acrcloud_access_key(self) -> str:
        return str(self._cfg("acrcloud_access_key", "") or "").strip()

    def acrcloud_access_secret(self) -> str:
        return str(self._cfg("acrcloud_access_secret", "") or "").strip()

