# CHANGELOG

## [1.0.4] - 2026-06-25

### Fixed
- 修复 `cover_card.py` 艺术家行解包 bug 导致封面卡片不生成
- 修复语音识歌误触发问题（关键字匹配改为仅 @bot + 语音触发）
- 修复非 CQ 平台（QQ 官方）语音消息无法获取的问题
- 修复 `file_to_base64` 导入路径适配 AstrBot v4.25+
- 修复酷狗分享链接标题解析混乱（`_` → ` - `）
- 修复 spotdl 搜索超时向用户报错（改为静默跳过）

### Changed
- 链接提取改为两阶段：通用 URL 正则 + 平台路由表匹配，覆盖更多分享格式
- 语音缓存支持本地路径（兼容非 OneBot 平台）
- ACRCloud 识别启用 BOTH 模式 + rec_length=12 + 文本元数据回退
- `llm_search_provider` 配置项改为 `select_provider` 下拉选择
- `_resolve_song_info` 封面缺失时用 yt-dlp 搜索缩略图作为回退

## [1.0.3] - 2026-06-22

### Added
- 语音识歌 LLM 工具 `recognize_song`：用户说"识歌""这是什么歌"并发送语音，LLM 自动调用 ACRCloud 识曲下载
- 新增 `enable_voice_recognition`、`acrcloud_host`、`acrcloud_access_key`、`acrcloud_access_secret` 配置项

### Changed
- 语音识歌改为 LLM 工具调用，不再硬编码 @bot 触发，更灵活自然
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