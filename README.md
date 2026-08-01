# astrbot_plugin_smart-collector

AstrBot 智能采集插件。一个配置面板内管理多个网站/API 数据源，并以专属指令、通用
`/爬取` 指令或 `smart_collect` LLM 工具触发采集。

## 功能

- 四类内容：视频、图片、音频、文字；未明确指定时按视频 → 图片 → 音频 → 文字选择。
- 网站、API 与 Pixiv 三种可重复配置模板，每个条目有独立名称、开关和专属指令。
- Pixiv 条目支持多 Tag 图片搜索、全年龄/R18/全部年龄段筛选，以及二维码 OAuth 登录。
- `asyncio` 并发采集；HTTP/2、重试、Cookie 轮换和 `curl-cffi` 浏览器 TLS 指纹回退。
- 缓存媒体文件和文字，命中时复用本地数据；去重支持关闭或永久去重。
- 首次成功后记录 HTML 选择器或 JSON 路径；结构失效时自动回退到启发式解析并更新画像。
- 可选图片转 PDF、图片/视频 ZIP 压缩、AES 压缩密码、OneBot 合并转发节点。
- 用户级限速、按天/周/月/星期定时发送、缓存自动清理。
- 可选择 AstrBot 已配置模型为文字生成摘要。
- 视频页没有直链时使用 `yt-dlp` 通用解析器作为回退。
- 图片候选会结合正文位置、`alt`/标题/图注、原图元数据、用户描述、尺寸和文件大小评分，自动降低图标、Logo、头像、广告和追踪图；下载后会验证真实图片格式与尺寸。
- 图片支持 `srcset` 最大规格、Open Graph、JSON/API 原图字段、懒加载属性和 CSS 背景图；用户可在指令后附加描述，例如 `/爬取 https://example.com 图片 蓝色夜景 横屏`。
- 自动识别同站数字分页并随机选择页面，失败时回退原页面；同优先级候选也会随机选择。
- 媒体使用流式下载，不再受页面响应的 100 MiB 内存缓冲上限。

反爬兼容层用于处理正常的 TLS/浏览器指纹检查和无需交互的 Cloudflare 页面，不绕过
CAPTCHA、登录、付费墙、访问控制或站点授权。使用者仍需遵守目标网站条款、robots 规则和
适用法律。

## 安装

在 AstrBot WebUI 中使用仓库地址安装：

```text
https://github.com/Lan-0v0/astrbot_plugin_smart-collector
```

AstrBot 会根据 `requirements.txt` 安装依赖。插件要求 AstrBot `>=4.10.4,<5`，因为配置面板
使用了 `template_list`。

## 使用

默认配置自带以下 API 条目：

| 字段 | 值 |
| --- | --- |
| 名称 | `Lanの默认配置` |
| URL | `https://api.yaohud.cn/api/v2/setu` |
| 请求头键 | `key` |
| 请求头值 | `RgDEYLevGRcMSNIF8z9` |
| 类型 | 图片 |
| 专属指令 | `/插画` |

该 API 实际要求旧式 `key` 查询参数。插件先按配置发送请求头；若 JSON 明确返回 401/403，
会在同一域名自动用同一键值重试查询参数，以兼容该接口。

常用触发方式：

```text
/插画
/pixiv
/pixiv 百合 JK 白丝
/pixiv登陆
/pixiv登陆 本地
/爬取 https://example.com/path
/爬取 https://example.com/path 图片
/爬取 https://example.com/path 图片 蓝色夜景 横屏
```

`/爬取` 的规范为 `/爬取 [URL] [类型]`，URL 必须提供，类型可选。它会把该地址作为临时
网站按“视频 → 图片 → 音频 → 文字”抓取；URL 前后的类型文字会限制目标类型。未提供有效
URL 时只返回指令规范提示，不会抓取已配置条目。图片描述会参与候选排序和尺寸约束；如果页面没有
足够的文字元数据，插件会继续使用正文位置、原图来源和图片质量进行回退。成功响应只发送抓取到的媒体或文字，不附加
数据源名称、缓存状态和来源地址。

Pixiv 条目首次使用时发送 `/pixiv登陆`，扫描二维码并在浏览器完成登录后，把回调地址作为
`/pixiv登陆 [URL]` 发送。登录凭据只保存为插件数据目录中的 Refresh Token；随后可用
`/pixiv [Tag1] [Tag2]` 或条目专属指令（例如 `/p 百合 JK 白丝`）搜索同时符合多个 Tag 的图片。
本地桌面部署可发送 `/pixiv登陆 本地`，插件会启动隔离的 Chrome/Edge 窗口，通过本机
DevTools 协议自动截获授权 code 并完成登录；二维码与手动 code 流程仍作为远程部署回退。

启用“自然语言爬取”后，AstrBot 的 LLM 可调用 `smart_collect`，参数包括需求文字、可选数据源
名称、可选内容类型和可选临时 URL。

定时发送需要先在对应条目勾选周期，并在目标会话中至少成功触发该条目一次。插件会记录
该会话作为发送目标。“每周”沿用首次订阅的星期，“每月”沿用首次订阅的日期。

## 配置说明

配置由 `_conf_schema.json` 驱动。网站模板在“去重”之前提供多项 Cookie；API 模板提供请求头
键和值。“视频画质”优先选择最低或最高分辨率，无法识别或指定格式下载失败时自动回退；
“指定发送QQ群”可填写多个群号，通过当前第一个启用的 OneBot 适配器按定时设置主动发送，
无需先在目标群触发插件。名称由 `display_item` 显示在条目折叠标题下方。“图片爬取忽略尺寸”
会忽略实际文件小于指定 KB 的图片，默认 100 KB，填写 `-1` 时关闭。该项与并发数、请求超时、
文字摘要、摘要人设和缓存清理均为位于整个自定义爬取项下方的全局设置；并发数和请求超时填写
`-1` 时不限制。

AstrBot v4 当前的 schema 条件只支持“字段严格等于固定值”，不能表达“多选列表非空”或
“模型提供商非空”。因此定时时间和摘要人设仍会显示；运行时只有周期非空、提供商非空时才
启用相应功能。压缩密码和自定义 QQ 号使用可表达的等值条件，会按开关正常显隐。

持久化数据位于 AstrBot 的：

```text
data/plugin_data/astrbot_plugin_smart_collector/
```

包含 SQLite 索引、解析画像、订阅记录和缓存文件；插件更新不会覆盖这些数据。
大文件会流式写入缓存以避免占用同等大小的内存，但最终发送仍受对应机器人平台的文件大小限制。

## 开发与测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
ruff check .
ruff format --check .
python scripts/live_smoke.py --pektino-only
python scripts/live_smoke.py --pektino-only --video-quality highest
```

联网烟测默认覆盖妖狐 API 和 Mukyu 随机图片；`--include-video` 添加 Avbebe，
`--include-pektino` 添加 Pektino 随机分页视频；`--pektino-only` 只访问 Pektino，适合快速回归。
目标站离线、DNS 污染或出口策略阻断会被明确报告为外部网络失败，不会被伪装为成功。

## 版本

当前版本：`v0.2.1`。变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

[MIT](LICENSE)
