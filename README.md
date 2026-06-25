# AnyMusic

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-blue)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/License-AGPLv3-green)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.4-blueviolet)](CHANGELOG.md)

## 简介

AstrBot 全平台音乐插件。自动识别主流音乐平台分享链接，通过 yt-dlp + spotdl 双引擎竞争匹配下载高质量音频，支持 LLM 智能选歌、语音识歌与点歌。

## 功能特点

### 支持的平台

| 平台 | 解析方式 |
|------|---------|
| Spotify | oEmbed API + HTML meta + JSON-LD |
| Apple Music | HTML `<title>` + JSON-LD |
| YouTube / YouTube Music | yt-dlp `--dump-json` |
| SoundCloud | yt-dlp `--dump-json` |
| Bilibili 音频 (`/audio/au\d+`) | yt-dlp `--dump-json` |
| 网易云音乐 | HTML `<title>` 通用解析 |
| QQ 音乐 | HTML `<title>` 通用解析 |
| 酷狗音乐 | HTML `<title>` 通用解析 |
| 酷我音乐 | HTML `<title>` 通用解析 |
| 咪咕音乐 | HTML `<title>` 通用解析 |
| 汽水音乐 | HTML `<title>` 通用解析 |

> Bilibili 仅识别纯音频链接 (`/audio/au\d+`)，不会拦截普通视频 (`/video/`)。
>
> 目前主要确保 Apple Music 和 Spotify 的可用性（因为本人在用），其他平台测试较少或无测试，欢迎提 Issue 或 PR。

### 核心特性

- ✅ **双引擎竞争下载** — yt-dlp + spotdl 并行搜索，通过多维评分选出最优音源
- ✅ **LLM 智能选歌** — 开启后 LLM 从候选中分析并选出最匹配的版本，杜绝翻唱/原唱误匹配
- ✅ **语音识歌** — 发送语音并说"识歌""这是什么歌"，LLM 自动调用 ACRCloud 听歌识曲后下载（需配置密钥）
- ✅ **时长精确匹配** — 解析原始歌曲时长，候选偏差超过阈值自动拒绝（默认 97% 精度可配置）
- ✅ **多维评分系统** — 时长匹配 40% + 标题相似度 40% + 来源加分 20% + 负面关键词过滤
- ✅ **信息卡片** — 深色风格卡片，展示专辑封面 + 歌曲信息，艺术家名自适应字号居中显示
- ✅ **跨平台字体** — 自动下载 Noto Sans SC 字体，Windows/Linux/macOS 均完美渲染中英文
- ✅ **LLM 点歌** — 说「我想听夜曲」「放一首晴天」，Bot 自动搜索下载
- ✅ **语音 + 文件双发送** — 独立配置仅语音 / 仅文件 / 同时发送
- ✅ **跨平台兼容** — 不支持语音的平台自动回退到文件发送
- ✅ **异步非阻塞** — 解析和下载均为异步，不阻塞 Bot 主循环
- ✅ **内存缓存** — 同一链接 10 分钟内复用解析结果
- ✅ **自动清理** — 发送完成后立即删除临时音频文件
- ✅ **多消息平台兼容** — 10 个 AstrBot 适配器均支持，不支持 Record 的平台自动回退到 File 发送

### 消息平台兼容性

| 平台适配器 | 图片卡片 | 语音(Record) | 文件(File) | 备注 |
|------|:---:|:---:|:---:|------|
| aiocqhttp | 支持 | 支持 | 支持 | QQ 完整支持 |
| qq_official | 支持 | 支持 | 支持 | 官方 QQ 机器人 |
| telegram | 支持 | 支持 | 支持 | 完整支持 |
| discord | 支持 | 不支持 | 支持 | 语音自动降级为文件 |
| kook | 支持 | 部分 | 支持 | 语音自动降级为文件 |
| slack | 支持 | 不支持 | 支持 | 语音自动降级为文件 |
| dingtalk | 支持 | 不支持 | 部分 | 语音自动降级为文件 |
| lark | 支持 | 不支持 | 支持 | 语音自动降级为文件 |
| mattermost | 支持 | 不支持 | 支持 | 语音自动降级为文件 |
| satori | 取决于后端 | 取决于后端 | 取决于后端 | 协议能力由后端实现决定 |

> 以上兼容性对照 AstrBot 官方文档 `ADAPTER_NAME_2_TYPE` 列表逐项确认。

## 安装

### 方法一：插件市场安装

1. 打开 AstrBot WebUI 管理面板
2. 进入「插件市场」
3. 搜索 `astrbot_plugin_anymusic` 或 `AnyMusic`
4. 点击安装

### 方法二：手动安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/AMYdd00/astrbot_plugin_anymusic.git
pip install -r astrbot_plugin_anymusic/requirements.txt
```

### 前置依赖

| 依赖 | 用途 |
|------|------|
| `yt-dlp` | YouTube 搜索 + 音频下载 |
| `spotdl` | Spotify ISRC 精确匹配 + YouTube Music 下载 |
| `ffmpeg` | 音频格式转换 |
| `beautifulsoup4` | HTML 解析（Apple Music / 国内平台） |
| `aiohttp` | 异步 HTTP 请求 |
| `Pillow` | 信息卡片图片生成 |
| `ffmpeg` | QQ 语音 AMR 转码（语音识歌必需） |
| `pyacrcloud` | ACRCloud 听歌识曲（语音识歌必需） |

```bash
pip install yt-dlp spotdl beautifulsoup4 aiohttp Pillow
```

## 配置说明

在 AstrBot WebUI -- 插件管理 -- AnyMusic -- 配置：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `proxy` | string | 空 | HTTP 代理地址，如 `http://127.0.0.1:7890` |
| `download_dir` | string | `data/music` | 临时下载目录 |
| `audio_format` | string | `mp3` | 音频格式 (mp3 / m4a / opus) |
| `audio_quality` | int | `192` | 音频质量 (kbps)，64~320 |
| `max_file_size_mb` | int | `50` | 最大文件大小限制 (MB) |
| `search_timeout` | int | `30` | 搜索超时 (秒) |
| `match_threshold` | int | `97` | 时长匹配精度 (%), 97 表示允许 +-3% 偏差 |
| `send_mode` | string | `都发送` | 发送方式：仅语音 / 仅文件 / 都发送 |
| `enabled_groups` | list | `[]` | 启用的群组，留空则所有群组生效 |
| `llm_search_enabled` | bool | `false` | 启用 LLM 智能选歌，从候选中分析并选出最匹配版本 |
| `llm_search_provider` | string | 空 | LLM 提供商 ID（需在 AI 配置中已添加），留空使用当前会话默认 |
| `enable_voice_recognition` | bool | `false` | 启用语音识歌，群内 @Bot + 语音即可识别 |
| `acrcloud_host` | string | 空 | ACRCloud 服务地址，如 `identify-eu-west-1.acrcloud.com` |
| `acrcloud_access_key` | string | 空 | ACRCloud Access Key |
| `acrcloud_access_secret` | string | 空 | ACRCloud Access Secret |
| `spotify_client_id` | string | 空 | Spotify Client ID，spotdl 搜索凭证（免费注册见下文） |
| `spotify_client_secret` | string | 空 | Spotify Client Secret，spotdl 搜索凭证 |

## 使用

1. 在 AstrBot 管理面板中启用插件
2. 在群聊中分享任意支持平台的音乐链接，Bot 自动解析并下载
3. 也可以直接对 Bot 说「放一首周杰伦晴天」，Bot 会搜索下载

### 首次使用

插件首次生成信息卡片时会自动从 GitHub 下载 Noto Sans SC 字体（约 30MB）存储到 `plugins/astrbot_plugin_anymusic/fonts/` 目录。此过程仅执行一次，后续即时可用。

### 语音识歌（可选）

插件支持通过 LLM 工具听歌识曲：发送语音消息并说"这是什么歌"、"帮我识歌"等，Bot 会自动识别语音中的歌曲并下载。

**前置准备**：需要注册 ACRCloud 账号获取免费 API 额度：

1. 访问 [ACRCloud 控制台](https://console.acrcloud.com) 注册账号（选择免费试用计划）
2. 创建新项目 → 选择 **Audio & Music Recognition**
3. 在「API Keys」页面获取：
   - `Access Key`（访问密钥）
   - `Access Secret`（访问密钥密码）
4. 根据账户区域选择 Host 地址：
   - 欧洲地区：`identify-eu-west-1.acrcloud.com`
   - 亚洲地区：`identify-ap-southeast-1.acrcloud.com`
5. 回到插件配置页面，填入以上四个参数并开启 `enable_voice_recognition`

> 免费额度为每天数百次识别，个人使用完全足够。
> 
> 注意：ACRCloud 对原曲片段识别精准，但对纯哼唱（无伴奏）支持较弱，建议发送包含原曲的语音。

### 配置 Spotify API 凭据（可选）

插件依赖 spotdl 进行 Spotify 歌曲搜索。由于 Spotify 收紧了未认证 API 请求限制，spotdl 4.5.0 开始需要配置 **Spotify Client ID 和 Client Secret** 才能正常工作。

**免费注册步骤**：

1. 访问 [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) 并登录你的 Spotify 账号（免费账号即可）
2. 点击右上角 **Create app** 按钮
3. 填写应用信息：
   - **App name**：任意名称，如 `AnyMusic`
   - **App description**：随意填写，如 `AstrBot music plugin`
   - **Website**：填写 `https://github.com/AMYdd00/astrbot_plugin_anymusic`
   - **Redirect URI**：填写 `http://localhost:8080/callback`（不会实际用到）
   - 勾选 I understand and agree with Spotify's Developer Terms of Service
4. 点击 **Save** 创建应用
5. 进入应用详情页，点击右上角 **Settings**
6. 找到 **Client ID** 和 **Client secret**（点击 View secret 查看）
7. 回到插件配置页面，将这两项填入 `spotify_client_id` 和 `spotify_client_secret`
8. 保存配置后，spotdl 搜索即可正常工作

> **注意**：
> - 免费账号每月有 API 调用次数限制，个人使用完全足够（每天数百次）
> - 如果不想配置，插件会自动跳过 spotdl 搜索，仅使用 yt-dlp（功能不受影响，只是少了一个搜索源）
> - 如遇问题，请检查代理设置是否允许访问 Spotify API 域名（`api.spotify.com`）

### 匹配精度说明

`match_threshold` 控制时长匹配的严格程度：
- `100` = 仅接受精确匹配（可能搜不到）
- `99` = 允许 +-1% 偏差（更严格）
- `97` = 允许 +-3% 偏差（默认，推荐）
- `90` = 允许 +-10% 偏差（宽松，可能误匹配 remix/版本变体）

## 工作原理

```
用户分享音乐链接
    │
    ▼
正则路由表识别平台
    │
    ├─ Spotify / Apple Music → 精解析 (时长/封面/专辑等)
    ├─ YouTube / SoundCloud / Bili → yt-dlp 提取元数据
    └─ 网易云 / QQ / 酷狗 / 酷我 / 咪咕 → HTML <title> 解析
    │
    ▼
生成磨砂玻璃风格信息卡片 → 发送图片
    │
    ▼
双引擎竞争下载 (yt-dlp + spotdl)
    │
    ├─ yt-dlp --dump-json ytsearch3:   ──┐
    ├─ spotdl save <query> --save-file  ──┤ 并行
    │                                     │
    ├─ 多维评分:                            │
    │   时长匹配 40% + 标题相似 40%           │
    │   + 来源加分 20% + 负面词惩罚 -50%       │
    │                                     │
    └─ 选最优 → 下载单个文件
    │
    ▼
按 send_mode 发送语音/文件 → 清理临时文件
```

## 技术细节

- **正则路由表**：`utils.py` 中维护 11 个平台的正则匹配规则，按优先级排序
- **双引擎并行**：使用 `asyncio.gather` 同时发起 yt-dlp 和 spotdl 元数据搜索
- **多维评分**：引入 `rapidfuzz` 对候选标题与原歌名做模糊匹配，过滤 `instrumental / karaoke / cover / remix / live` 等非原曲变体
- **spotdl ISRC 匹配**：通过 Spotify 元数据提取 ISRC 码，在 YouTube Music 官方音轨库中精确匹配
- **HTML 标题解析**：国内平台无结构化 API，统一解析 `<title>` 并自动去除平台后缀（如 "- 网易云音乐"）
- **录制回退**：不支持 Record 消息的平台自动降级为 File 发送

## 常见问题

**Q: 为什么下载到纯音乐 / 伴奏 / 翻唱？**

多维评分系统会自动过滤带有 `instrumental / karaoke / cover / remix` 等关键词的候选。如果仍出现误匹配，可调高 `match_threshold`（如 100）。

**Q: 为什么不直接从 Apple Music / Spotify 下载？**

Apple Music 有 DRM 加密保护，Spotify 需要 Premium 账户。本插件通过 ISRC 匹配 YouTube Music 官方音轨，绝大多数情况能下载到原曲录音室版本。

**Q: 国区 Apple Music 链接能识别吗？**

能。支持 `music.apple.com/cn/`、`/us/`、`/jp/` 等所有区域。

**Q: 为什么显示「无法识别该音乐链接」或「未找到匹配的歌曲」？**

可能原因：链接格式不被正则路由表匹配；解析元数据时网络超时（检查代理设置）；候选音源时长偏差超过 `match_threshold` 阈值（可适当降低阈值）。

## 文件结构

```
astrbot_plugin_anymusic/
├── _conf_schema.json          # WebUI 配置定义
├── config.py                  # ConfigHelper
├── cover_card.py              # 信息卡片生成 (PIL) + 字体自动下载
├── downloader.py              # 双引擎竞争下载 (yt-dlp + spotdl)
├── logo.png                   # 插件图标
├── main.py                    # 插件入口 + 路由分发
├── metadata.yaml              # 元数据
├── README.md                  # 本文件
├── requirements.txt           # 依赖列表
├── utils.py                   # URL 路由表 + HTML 标题解析 + ResultCache
└── parsers/
    ├── __init__.py
    ├── apple_music.py          # Apple Music 精解析器
    └── spotify.py              # Spotify 精解析器
```

## 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)

## 作者

- [AMYdd00](https://github.com/AMYdd00)

> 本插件代码完全由 AI 生成。

## 相关链接

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) - 一个易于上手的多平台 LLM 对话机器人框架
- [QQ 群](https://qm.qq.com/q/cOrzqdkW7m) - 欢迎提交 Issue 和加群反馈