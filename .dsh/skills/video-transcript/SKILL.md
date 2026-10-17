---
name: video-transcript
description: 把抖音/小红书视频转成 Markdown 文字稿（yt-dlp 取音 + 本机离线 ASR，免费、不上传音频）。当用户给出抖音/小红书分享链接或整段分享文案、说「转写/提取文案/转文字/扒文案/做字幕/把这条视频存成笔记」，或要把短视频内容沉淀到 Markdown/Obsidian 时使用本技能。
whenToUse: 用户发来 v.douyin.com / douyin.com / xiaohongshu.com / xhslink.com 链接或带这类链接的分享文案；要求把视频转成文字、提取口播文案、做会议/课程记录；要求转写本机音视频文件（mp4/m4a/mp3/wav）。
---

# 视频转写 · vtc

本机已装好一条完整流水线，**不要重新造轮子，直接用 `vtc` 命令**。

## 一条命令搞定

```bash
# 工具位置：/storage/emulated/0/AI-Workspace/03_技能库/video-transcript/vtc
VTC=/storage/emulated/0/AI-Workspace/03_技能库/video-transcript/vtc

$VTC "分享链接或整段分享文案"          # 抖音/小红书
$VTC --timestamps "链接"                # 额外输出 [mm:ss] 逐句时间轴
$VTC -o /storage/emulated/0/AI-Workspace/04_素材库/视频转写 "链接"
$VTC /path/to/video.mp4                 # 本地音视频文件也行
```

- 整段分享文案可以直接粘（会自动抠出链接），不用自己截取 URL。
- 默认输出到 `04_素材库/视频转写/`（工作区内），文件名 `<时间戳> <标题>.md`。
- ASR 模型在 `.ai-workspace/vtc-models/`（工作区内，勿提交）。
- 全流程本机完成：**不花钱、不用 API Key、音频不上传**。

## 平台机制（关键，决定排错方向）

| 平台 | 通道 | 依赖 |
|---|---|---|
| **抖音** | **官方嵌入播放器白名单通道** | `vtc.py` 的 `douyin_*` 函数。带 `Referer: https://open.douyin.com/` 调 `www.douyin.com/aweme/v1/web/aweme/detail/` 即返回完整详情——**无需 cookie / 签名 / uifid**。失败会自动退回 yt-dlp |
| **小红书** | yt-dlp + cookie | 需要 `cookies-xhs.txt`（已完成，自动导出）。链接**必须带 `xsec_token`**（App 分享出来的都带） |
| 本地文件 | ffmpeg → ASR | 无网络依赖 |

## 排错

| 现象 | 处理 |
|---|---|
| 抖音报 `Uifid Not Found` | 官方 Referer 没带上；检查 `douyin_official_detail()` 的请求头 |
| 小红书 `No video formats found!` | cookie 过期（约 2–4 周）。在内置浏览器打开并登录小红书 → **关掉标签页**（cookie 这时才刷盘）→ `vtc --export-cookies xhs` |
| `Output file does not contain any stream` | 选中了无音轨的 DASH 分片；已内置自动重取，若仍报错看 `to_pcm16k` 的格式选择器 |
| 网络类报错（TLS/超时） | 设备 DNS 被上游网关劫持过。已把「专用DNS」设为 `dns.alidns.com` 修复；换网络后如复发，改用 `dnsbypass/` 或重设私人 DNS |
| 模型缺失 | `vtc --fetch-models` |
| 下载慢 | `pget <URL> <输出文件> [连接数]` 多连接分段下载（实测快约 11 倍） |

## 性能参考（实测）

本机离线 SenseVoice，**RTF ≈ 0.25**（比实时快约 4 倍）：
- 5:20 的抖音视频 → **26 秒**出 1173 字
- 3:03 的小红书视频 → **17 秒**出 463 字
- 支持中英日韩粤混说，自带标点与数字规范化

## 想改配置

`video-transcript-native/config.json`：
- `asr.backend`: `local`（默认，离线）或 `cloud`（任意 OpenAI 兼容的 `/audio/transcriptions`）
- `output_dir` / `keep_audio` / `proxy` / `ytdlp_extra_args`

## 代码与文档位置

- 主程序：`video-transcript-native/vtc.py`
- 完整说明：`video-transcript-native/README.md`（4.1–4.5 节讲网络与平台机制）
- 产出示例：`video-transcript-native/examples/`
- 全局命令：`vtc`（装在 `$PREFIX/bin`，任何目录可用）

## 注意

- 抖音官方通道走的是平台自身的嵌入播放能力，不要改成对抗性抓取。
- 转写内容属用户素材，输出只落本机；不要外发。
