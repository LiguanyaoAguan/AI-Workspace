# vtc —— 视频文案采集器 · Pad 原生版

把**抖音 / 小红书视频**变成 **Markdown 文字稿**，全程在小米 Pad 本机完成，
**不依赖桌面浏览器，也不需要装那个 Chrome 扩展**。

转写默认走**本地离线模型**（sherpa-onnx + SenseVoice），零密钥、零费用、不联网上传。

---

## 1. 为什么会有这个项目

原始方案 [Yinmu/video-transcript-clipper](https://github.com/Yinmu/video-transcript-clipper)
是一个 Chrome/Edge 扩展（Manifest V3），只能在桌面浏览器的「开发者模式 → 加载已解压的扩展程序」里装。
安卓版 Chrome / Firefox 都不支持安装任意扩展，所以在这个 Pad 上装不了。

但那个扩展真正干的活，**不需要浏览器扩展也能干**：

| 扩展的做法 | 本项目的做法 |
|---|---|
| 在页面里 hook 网络请求，捞真实媒体地址 | `yt-dlp` + cookie 提取（抖音/小红书都要求新鲜 cookie） |
| 把音频上传到用户的云端 ASR | **本机 SenseVoice 离线识别**，音频不出设备 |
| 导出 Markdown / Obsidian URI | 直接写 Markdown 文件（带 frontmatter） |

换来的好处：命令行可批量、可接进知识库、不需要桌面电脑、不用担心扩展随平台改版失效。

---

## 2. 已装好的环境

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | 3.14.6 | 系统自带 |
| yt-dlp | 2026.08.19 | `pip install -U yt-dlp` |
| ffmpeg / ffprobe | 8.1.3 | 完整构建，含 libmp3lame/opus/x264/mediacodec |
| numpy | 2.4.4 | sherpa-onnx 的依赖 |
| sherpa-onnx | 1.13.8 | **官方 Android aarch64 轮子**，带 SenseVoice + Silero VAD |
| SenseVoice int8 | 155 MB | 中英日韩粤，中文效果好 |

模型位置：`~/.cache/vtc-models/`

---

## 3. 快速开始

> `/storage/emulated/0` 是 **noexec** 挂载，所以项目目录里的 `./vtc` 不能直接执行。
> 已把启动器装到可执行分区：**直接用全局命令 `vtc`**（或在项目里 `bash vtc`）。

```bash
# 直接给链接（或 App 分享出来的整段文案）
vtc "https://v.douyin.com/xxxx/"
vtc "3.14 复制打开抖音，看看【某人的作品】 https://v.douyin.com/xxxx/"

# 小红书（记得带上 App 分享链接里的 xsec_token）
vtc "https://www.xiaohongshu.com/explore/xxxx?xsec_token=xxx"

# 也可以直接转本机音视频文件
vtc ~/Movies/某个视频.mp4

# 附带时间轴逐句版 / 换输出目录
vtc --timestamps -o ~/notes "https://..."

# 首次使用或换机：下载模型
vtc --fetch-models
```

输出：`output/<时间戳> <标题>.md`

常用参数：

| 参数 | 作用 |
|---|---|
| `-o, --out DIR` | 输出目录 |
| `--timestamps` | 额外输出 `[mm:ss]` 逐句版 |
| `--backend local\|cloud` | 换 ASR 后端 |
| `--keep-audio` | 保留中间 WAV（便于核对） |
| `--fetch-models` | 下载/检查本地 ASR 模型 |
| `--export-cookies PLATFORM` | 从内置浏览器导出 cookie |
| `--print-config` | 打印当前配置 |

---

## 4. ⚠️ 平台限制（必读）

**抖音和小红书现在都要求新鲜 cookie**，这是平台反爬，不是本工具的缺陷。
没有 cookie 时的表现：`No video formats found!` 或 `Fresh cookies ... are needed`。

### 4.1 网络层：DNS 旁路（已内置，无需配置）

**症状**：`SSL: UNEXPECTED_EOF_WHILE_READING` —— 对 `www.douyin.com` 的 TLS 握手被重置。

```
$ curl -sS https://www.douyin.com/
curl: (35) TLS connect error: error:0A000126:SSL routines::unexpected eof while reading
```

**根因（本机实测）**：所连 Wi-Fi 的**上游网关劫持了全部明文 DNS** ——
不只是 114.114.114.114，连 8.8.8.8 / 1.1.1.1 都被改写成 `198.18.x.x` 段的 fake-IP，
再把发往这些 fake-IP 的流量转去境外节点。抖音重置境外 IP，于是握手失败。

> ⚠️ 注意：设备上**没有任何代理 App，也没有 VPN**（`NOT_VPN`）。所以「改手机上的代理规则」
> 是改不了的 —— 问题在网络上游。这一点排查了很久才确认。

**关键实测**：该代理**只拦截 fake-IP 目的地**。拿公共 DoH 解析出真实 IP 后直连，
`www.douyin.com` 立刻恢复正常：

```
101.73.110.83   HTTP 200 | 72914 B | TLS 0.046s
101.73.110.169  HTTP 200 | 72914 B | TLS 0.053s
```

**本项目的处理**：`dnsbypass/` 给 `socket.getaddrinfo` 打补丁，命中名单的域名改走 DoH。
`vtc` 启动时自动装载，并通过 `PYTHONPATH` 传递给 yt-dlp 子进程。

> 设好 4.2 的私人 DNS 之后，这层补丁已属冗余（但保留无害，换到别的被劫持网络时仍能兜底）。

### 4.2 想让整个设备都好（含内置浏览器）：改「私人 DNS」

Python 层补丁只对 CLI 生效。内置浏览器（WebView）走系统解析器，拿到的仍是 fake-IP。
由于网关劫持的是**明文 DNS（53 端口）**，而 **DoT（853 端口）是通的且能拿到真实 IP**：

| DoT 服务器 | www.douyin.com 解析结果 |
|---|---|
| `dns.alidns.com` | `101.73.110.x` ✅ 真实国内 IP |
| `dot.pub` | `101.73.110.x` ✅ |
| `dns.google` | `155.102.176.x`（境外） |

**操作（本机已完成 ✅）**：设置 → 更多连接 → 专用DNS → 选「专用 DNS 提供商主机名」→ 填 `dns.alidns.com`

> 本机当前状态：`private_dns_mode=hostname`，`private_dns_specifier=dns.alidns.com`，
> 实测全部域名已恢复真实解析（`www.douyin.com -> 101.73.110.x`）。
> 想还原：同一页面改回「自动」即可。

> 只改「Wi-Fi 的静态 DNS」**没用** —— 53 端口被整段劫持，必须用加密 DNS。

### 4.3 抖音：走官方嵌入播放器通道（已解决 ✅）

抖音有 `Argus` 风控：Web 接口要求 `a_bogus` 签名 + 会话 `uifid`。以下是本机实测的各方案：

| 方案 | 结果 |
|---|---|
| yt-dlp 直接解析 | ❌ 服务端请求被「验证码中间页」拦截（`sec_sdk_build/captcha/index.js`） |
| Node bdms 签名器出 `a_bogus` | ❌ 签名本身有效（168 字符，过了签名校验），但缺 `uifid`：<br>`Blocked by ArgusSecurityPlugin Uifid Not Found` |
| `ttwid` 注册接口 + 手工 cookie | ❌ 仍报 `Fresh cookies ... are needed` |
| 抖音分享页 `iesdouyin.com` | ❌ 返回「抱歉出错了 请尝试在抖音内观看」 |
| `f2` 库（社区推荐） | ❌ 装不了（`cryptography` 需 Rust 编译） |
| 内置浏览器 hook（仿原扩展） | ❌ WebView 的 Chromium DNS 缓存僵在旧 fake-IP，连不上 `www.douyin.com` |
| **官方嵌入播放器通道** ⭐ | ✅ **已跑通，见下** |

#### 解法：抖音给自己官方嵌入播放器开了风控白名单

翻抖音开放平台文档时找到两个官方设施：

**① 官方接口（文档原文：「该接口无需申请权限」）**

```
GET https://open.douyin.com/api/douyin/v1/video/get_iframe_by_video?video_id=<id>
```

只吃一个 `video_id`，**无任何鉴权**，返回视频标题、宽高和官方嵌入播放器地址：

```json
{"data":{"iframe_code":"<iframe src=\"https://open.douyin.com/player/video?vid=7696124176125529395\">",
         "video_title":"我的超能力是：…","video_width":2560,"video_height":1440}}
```

**② 官方播放器页面内部调的正是风控接口**

把播放器的 JS bundle（`video.*.js`）拆开看，它会请求：

```
https://www.douyin.com/aweme/v1/web/aweme/detail/   ← 和 yt-dlp 撞的是同一个接口
```

关键实测差别在于请求头：

```
不带 Referer          → HTTP 403  Blocked by ArgusSecurityPlugin Uifid Not Found
Referer: https://open.douyin.com/  → HTTP 200  完整 aweme_detail（89 KB）
```

**即抖音风控把「自家官方嵌入播放器」的来源加进了白名单。**
于是带上官方 Referer + Origin，就能直接拿到 `video.play_addr.url_list` ——
**无需 cookie、无需 a_bogus 签名、无需 uifid。**

下载 CDN 时再带上 `Referer: https://www.douyin.com/` 即可（否则 403）。

> 这条路完全走的是抖音自己的官方设施（官方接口 + 官方播放器 + 官方 CDN），
> 属于平台自身开放的嵌入播放能力，不是对抗性爬取。

代码见 `vtc.py` 的 `douyin_*` 系列函数；失败时自动退回 yt-dlp。

### 4.4 拿 cookie 的两种方式（小红书需要）

**方式 A（推荐）：从 DSH 内置浏览器导出**

内置浏览器（WebView 工作台）的 cookie 库是**明文存储**的，实测可直接读：

```bash
# 1. 在 DSH 内置浏览器里打开并登录 www.xiaohongshu.com / www.douyin.com
# 2. ⚠️ 关掉那个标签页（cookie 这时才刷到磁盘，实测确认）
# 3. 导出
vtc --export-cookies all
# 生成 cookies-xhs.txt / cookies-douyin.txt
```

实测小红书访客 cookie 就能导出 11 条（`a1` / `webId` / `gid` / `websectiga` / `xsecappid` 等）。

不配置也没关系——`vtc` 每次运行会自动尝试导出并使用。

> 备注：cookie 库位于 `/data/data/com.dsharnessmobile.shell/app_webview/*/Cookies`，
> 是标准 Chromium SQLite 格式，`value` 字段明文。`vtc` 会自动扫描所有 Profile 并按域名筛选。

**方式 B：自己提供 cookies.txt**

用任意方式导出 Netscape 格式的 cookies.txt，然后：

```json
{ "cookies_file": "/path/to/cookies.txt" }
```

### 4.5 备选：本地文件通道（任何平台都能用）

在抖音 App 里把视频**保存到相册**（或录屏），然后直接转本地文件：

```bash
vtc /storage/emulated/0/DCIM/Camera/xxx.mp4
```

---

## 5. 配置

配置文件：本目录下的 `config.json`（首次运行自动生成）

```json
{
  "asr": {
    "backend": "local",
    "local": { "num_threads": 4, "language": "zh", "use_itn": true },
    "cloud": {
      "base_url": "https://api.siliconflow.cn/v1",
      "api_key": "",
      "api_key_env": "SILICONFLOW_API_KEY",
      "model": "FunAudioLLM/SenseVoiceSmall"
    }
  },
  "model_dir": "~/.cache/vtc-models",
  "output_dir": "./output",
  "keep_audio": false,
  "ytdlp_extra_args": [],
  "cookies_file": "",
  "proxy": ""
}
```

### ASR 后端

**local（默认）** — 本机 SenseVoice 离线识别
- 优点：免费、离线、隐私（音频不出设备）、无并发限制
- 缺点：受 CPU 限制，长视频慢一些

**cloud** — 任意 OpenAI 兼容的 `/audio/transcriptions` 接口
- 已内置硅基流动（SiliconFlow）预设，其 `SenseVoiceSmall` 免费
- 也可指向 DashScope 兼容模式等其他厂商
- 填 `api_key` 或设环境变量

---

## 6. 工作原理

```
输入（链接 / 整段分享文案）
   │  regex 抠出链接 + 识别平台
   ▼
yt-dlp  ──  带 cookie 取真实媒体地址，下载最佳音轨
   │
   ▼
ffmpeg  ──  转 16kHz 单声道 PCM WAV
   │
   ▼
sherpa-onnx SenseVoice (+ Silero VAD 切句)
   │  逐段识别，保留每段起始时间
   ▼
Markdown（YAML frontmatter + 正文 + 可选时间轴）
```

---

## 7. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `No video formats found!` | 缺 cookie。跑 `vtc --export-cookies all`，先在内置浏览器登录 |
| `TLS connect error ... unexpected eof` | 代理把 `www.douyin.com` 路由到境外了，改直连 |
| `Fresh cookies ... are needed` | 抖音 cookie 过期，重新登录后导一次 |
| `模型目录不存在` | 跑 `vtc --fetch-models` |
| `dlopen failed: cannot locate symbol` | sherpa-onnx 原生库没找到；`vtc` 入口脚本会自动设 `LD_LIBRARY_PATH`，请用 `vtc` 而不是直接 `python3 vtc.py` |
| 识别结果为空 | 纯音乐/无人声，或模型没加载好；试 `--keep-audio` 留 WAV 核对 |
| 下载特别慢 | 用 `pget <URL> <OUT> [连接数]` 多连接分段下载（本机实测快约 11 倍） |

---

## 8. 本机环境修复记录

这个环境是**重定位过的 Termux**（前缀从 `/data/data/com.termux/files/usr`
改到 `/data/data/com.dsharnessmobile.shell/files/usr`），但二进制包没有重新编译，
`apt` 因此是坏的。已修好，改动了 2 个文件：

1. **`$PREFIX/bin/dpkg`**（原文件备份为 `dpkg.dsh-orig`）
   apt 传给 dpkg 的 PATH 是编译期旧路径 `/data/data/com.termux/files/usr/bin`，
   该目录不存在 → dpkg 找不到 `sh`/`rm`/`tar`/`diff`/`dpkg-deb`/`start-stop-daemon`。
   **修法**：包装脚本里 exec 前显式 `export PATH="$B/bin:/system/bin"`。

2. **新增 `$PREFIX/bin/dsh-apt`**（安装修复器）
   自动处理另外两个坑：
   - `.deb` 内成员路径带旧前缀，dpkg 会挂到 `$PREFIX/data/data/com.termux/...`
     → 安装后把「脱轨树」合并回 `$PREFIX`
   - `postinst` 等脚本 shebang 写死旧前缀的 sh，内核直接 ENOENT
     → 改写 `$PREFIX/var/lib/dpkg/info/` 下脚本的 shebang

   以后装包请用：`dsh-apt install -y <包名>`

3. **新增 `$PREFIX/bin/pget`** — 多连接并行分段下载器。

---

## 9. 待办 / 已知不足

- [x] DNS 旁路（解决网关 fake-IP 劫持）—— 已内置进 vtc
- [x] 本地离线 ASR（中英日韩粤）—— 已实测
- [x] 本地文件 → Markdown —— 已实测
- [x] 内置浏览器 cookie 导出 —— 已实测（小红书 14 条）
- [x] Node bdms 签名器生成有效 `a_bogus` —— 已实测（168 字符）
- [x] **抖音端到端** —— ✅ **已跑通真实视频**：`7696124176125529395`（AI 短片 5:20）
      官方嵌入播放器通道 → 1173 字 Markdown，**全程 26 秒**（见 4.3）
- [x] **小红书端到端** —— ✅ **已跑通真实笔记**：`6aaf3eb6...`（健身视频 3:03）
      经 yt-dlp 下载 → SenseVoice 转写 → 463 字 Markdown，**全程 17 秒**
      （关键：cookie + `xsec_token`；另需选带音轨的 `mp4` 档，EF5 的 `.m4s` 是 video-only 分片）
- [ ] 把 a_bogus 签名链整合进 `vtc`（目前是独立验证脚本，在 `~/douyin-abogus/vtc_sign_test.js`）
- [ ] 未加说话人分离（sherpa-onnx 支持，可后续加）
- [ ] 未加字幕文件导出（SRT / VTT）

---

## 10. 许可与致谢

本项目代码可自由使用。依赖的第三方：

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — Unlicense
- [FFmpeg](https://ffmpeg.org/) — LGPL/GPL
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) — Apache-2.0
- [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) — 模型权重见其许可

灵感来自 [Yinmu/video-transcript-clipper](https://github.com/Yinmu/video-transcript-clipper)（MIT）。
