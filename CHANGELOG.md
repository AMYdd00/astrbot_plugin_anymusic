# CHANGELOG

## [1.0.3] - 2026-06-22

### Added
- 语音识歌：群内 @Bot 发送语音消息，通过 ACRCloud 识别歌曲并自动下载
- 新增 `enable_voice_recognition`、`acrcloud_host`、`acrcloud_access_key`、`acrcloud_access_secret` 配置项

### Changed
- 依赖新增 `pyacrcloud` 和 `rapidfuzz`
- 插件描述更新

## [1.0.2] - 2026-06-21

### Added
- LLM 智能选歌：从候选音源中由 LLM 选出最匹配的版本，解决翻唱/原唱误匹配问题
- 新增 `llm_search_enabled` 和 `llm_search_provider` 配置项

### Changed
- 信息卡片艺术家排版优化：居中显示，自适应字号（30→18px），超长显示省略号
- 插件描述更新

### Fixed
- 艺术家名称过长时截断不美观的问题

## [1.0.1] - 2026-06-16

### Changed
- LLM 点歌工具支持独立发送模式配置（新增 `llm_tool_mode` 配置项）
- LLM 点歌时默认发送图片+语音而非仅语音
- 默认匹配精度从 99% 调整为 97%
- yt-dlp 版本要求提升至 >=2026.6

### Added
- 下载失败时返回详细的中文错误信息（反爬拦截、网络超时等）
- 网易云音乐 163cn.tv 短链接支持
- 汽水音乐（抖音旗下）平台支持

### Fixed
- Linux 容器内中文字体渲染问题（自动下载 Noto Sans SC）
- Apple Music 中区标题解析（支持书名号格式）
- spotdl 搜索参数错误导致双引擎完全失败

## [1.0.0] - 2026-05-08

### Added
- 初始发布
- 支持 Apple Music / Spotify 链接识别与元数据解析
- yt-dlp 搜索下载音频
- 磨砂玻璃风格信息卡片生成
- LLM 点歌工具