#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vtc —— 视频文案采集器 · Pad 原生版

把抖音 / 小红书视频变成 Markdown 文字稿，全程在本机完成，不依赖桌面浏览器扩展。

流水线：
    yt-dlp 抓真实媒体地址并下载音频
        -> ffmpeg 转 16kHz 单声道 PCM
        -> ASR（默认本地 sherpa-onnx SenseVoice，零密钥零费用）
        -> 写出带 frontmatter 的 Markdown

用法：
    ./vtc "https://v.douyin.com/xxxx/"
    ./vtc "7.85 复制打开抖音，看看【某某的作品】... https://v.douyin.com/xxxx/"
    ./vtc --timestamps "https://www.xiaohongshu.com/explore/xxxx"
    ./vtc -o ~/notes "https://..."
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
DEFAULT_MODEL_DIR = Path.home() / ".cache" / "vtc-models"

# ---------------------------------------------------------------- 常量

URL_RE = re.compile(r"https?://[^\s\"'<>）)，,、；;]+")

PLATFORM_HOSTS = {
    "douyin": ("douyin.com", "iesdouyin.com"),
    "xhs": ("xiaohongshu.com", "xhslink.com", "xhslink.cn"),
}

SHARE_NOISE = re.compile(
    r"(复制此链接|打开抖音|看看【|的作品】|复制打开抖音|复制本条信息|打开小红书|"
    r"长按复制|^[0-9.]+ ?|%s)"
)

DEFAULT_CONFIG = {
    "asr": {
        "backend": "local",
        "local": {
            "num_threads": 4,
            "language": "zh",
            "use_itn": True,
        },
        "cloud": {
            "base_url": "https://api.siliconflow.cn/v1",
            "api_key": "",
            "api_key_env": "SILICONFLOW_API_KEY",
            "model": "FunAudioLLM/SenseVoiceSmall",
            "timeout": 300,
        },
    },
    "model_dir": str(DEFAULT_MODEL_DIR),
    "output_dir": str(HERE / "output"),
    "keep_audio": False,
    "ytdlp_extra_args": [],
    "cookies_file": "",
    "proxy": "",
}


# ---------------------------------------------------------------- 环境自愈

DNSBYPASS_DIR = HERE / "dnsbypass"


def install_dns_bypass() -> bool:
    """启用 DNS 旁路。

    本机所连 Wi-Fi 的上游网关劫持了明文 DNS（所有公共 DNS 都被改写成
    198.18.x.x 段 fake-IP），并把发往这些 fake-IP 的流量转去境外节点，
    导致抖音等域名 TLS 被重置。

    实测该代理只拦截 fake-IP 目的地：拿真实 IP 直连就完全正常。
    这里在进程内给 socket.getaddrinfo 打补丁，命中名单的域名改走 DoH。

    子进程（yt-dlp）靠 PYTHONPATH 指向 dnsbypass 目录生效 —— 见 spawn_env()。
    """
    if not DNSBYPASS_DIR.is_dir():
        return False
    p = str(DNSBYPASS_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)
    try:
        import dnsbypass  # noqa: F401  （import 即 install）
        return True
    except Exception:
        return False


def spawn_env() -> dict:
    """给子进程（yt-dlp / ffmpeg）准备环境：带上 DNS 旁路。"""
    env = dict(os.environ)
    if DNSBYPASS_DIR.is_dir():
        cur = env.get("PYTHONPATH", "")
        p = str(DNSBYPASS_DIR)
        env["PYTHONPATH"] = p + (os.pathsep + cur if cur and p not in cur.split(os.pathsep) else "")
    return env


def ensure_native_libs() -> None:
    """sherpa-onnx 的原生库 RUNPATH 指向编译期旧前缀，必须靠 LD_LIBRARY_PATH 兜底。

    LD_LIBRARY_PATH 只在进程启动时被动态链接器读取，运行时改 os.environ 无效，
    所以这里发现缺失就带着新值重新 exec 自己一次。
    """
    if os.environ.get("_VTC_REEXEC") == "1":
        return
    try:
        import site

        site_dirs = []
        try:
            site_dirs.extend(site.getsitepackages())
        except Exception:
            pass
        try:
            site_dirs.append(site.getusersitepackages())
        except Exception:
            pass
    except Exception:
        site_dirs = []

    wanted = []
    for d in site_dirs:
        p = Path(d) / "sherpa_onnx" / "lib"
        if p.is_dir():
            wanted.append(str(p))
    wanted.append(os.environ.get("PREFIX", "/data/data/com.dsharnessmobile.shell/files/usr") + "/lib")

    cur = [p for p in os.environ.get("LD_LIBRARY_PATH", "").split(":") if p]
    if all(w in cur for w in wanted):
        return

    merged = wanted + [p for p in cur if p not in wanted]
    os.environ["LD_LIBRARY_PATH"] = ":".join(merged)
    os.environ["_VTC_REEXEC"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ---------------------------------------------------------------- 配置

def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️  config.json 解析失败（{e}），改用默认配置", file=sys.stderr)
            user = {}
    else:
        user = {}
        write_config(DEFAULT_CONFIG)
        print(f"ℹ️  已生成默认配置：{CONFIG_PATH}")
    return deep_merge(DEFAULT_CONFIG, user)


def write_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------- 输入解析

def resolve_input(text: str):
    """判断输入是本地媒体文件还是平台链接。

    返回 (kind, value, platform)，kind 为 "file" 或 "url"。
    """
    raw = (text or "").strip().strip('"').strip("'")
    if raw:
        p = Path(os.path.expanduser(raw))
        try:
            if p.is_file():
                return "file", p, ""
        except OSError:
            pass
    url, plat = extract_url(text)
    return "url", url, plat


def extract_url(text: str) -> tuple[str, str]:
    """从分享文案里抠出视频链接，返回 (url, platform)。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("没有输入内容")

    candidates = URL_RE.findall(text)
    if not candidates and re.match(r"^[\w.-]+\.[a-z]{2,}/", text, re.I):
        candidates = ["https://" + text.lstrip("/")]

    for u in candidates:
        u = u.rstrip("。，,、；;！!？?")
        low = u.lower()
        for plat, hosts in PLATFORM_HOSTS.items():
            if any(h in low for h in hosts):
                return u, plat

    if candidates:
        raise ValueError(
            "链接不是抖音/小红书：\n  " + "\n  ".join(candidates[:3])
        )
    raise ValueError(f"没找到链接，输入是：{text[:80]}")


# ---------------------------------------------------------------- cookie 导出

# DSH 内置浏览器（WebView 工作台）的 cookie 库所在位置。
# 实测：这些库是明文存储的（value 字段直接可读，encrypted_value 为空），
# 所以只要在内置浏览器里登录过一次，就能把 cookie 导出来喂给 yt-dlp。
WEBVIEW_ROOTS = [
    Path("/data/data/com.dsharnessmobile.shell/app_webview"),
    Path.home().parent / "app_webview",
]

COOKIE_DOMAINS = {
    "douyin": ("douyin.com", "iesdouyin.com"),
    "xhs": ("xiaohongshu.com", "xhslink.com", "xhslink.cn"),
}

# Chromium 的 expires_utc 是「1601-01-01 起的微秒数」，Netscape 要 Unix 秒
_EPOCH_DELTA = 11644473600


def export_cookies(platform: str, out_path: Path, quiet: bool = True) -> int:
    """从内置浏览器 cookie 库导出指定平台的 cookies.txt（Netscape 格式）。

    返回导出的 cookie 条数；0 表示没找到（即还没在那个浏览器里登录过）。
    """
    import shutil
    import sqlite3
    import tempfile

    want = COOKIE_DOMAINS.get(platform, ())
    if not want:
        raise ValueError(f"未知平台：{platform}")

    dbs: list[Path] = []
    for root in WEBVIEW_ROOTS:
        if root.is_dir():
            dbs.extend(sorted(root.glob("*/Cookies")))
    if not dbs:
        return 0

    lines = ["# Netscape HTTP Cookie File",
             "# 由 vtc 从 DSH 内置浏览器 cookie 库导出",
             ""]
    n = 0
    tmp = Path(tempfile.mkdtemp(prefix="vtc-ck-"))
    try:
        for db in dbs:
            try:
                snap = tmp / f"{db.parent.name}.db"
                shutil.copy2(db, snap)
                con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
                rows = con.execute(
                    "SELECT host_key, name, value, path, expires_utc, "
                    "is_secure, is_httponly, has_expires FROM cookies"
                ).fetchall()
                con.close()
            except Exception:
                continue

            for host, name, value, path, exp, sec, http, has_exp in rows:
                if not host or not any(host.endswith(d) for d in want):
                    continue
                if value is None or value == "":
                    continue  # 加密的（本机实测不会出现）
                if has_exp and exp:
                    expiry = int(exp / 1_000_000) - _EPOCH_DELTA
                else:
                    expiry = 0  # 会话 cookie
                dom = host if host.startswith(".") else host
                flag = "TRUE" if host.startswith(".") else "FALSE"
                if http:
                    dom = "#HttpOnly_" + dom
                    flag = "TRUE" if host.startswith(".") else "FALSE"
                lines.append("\t".join([
                    dom, flag, path or "/",
                    "TRUE" if sec else "FALSE",
                    str(expiry), name, value,
                ]))
                n += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if n:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out_path.chmod(0o600)
    return n



# ---------------------------------------------------------------- 抖音官方通道

# 这条通道是本次逆向的关键成果，全程走抖音自己的官方设施，不碰风控：
#
#   ① 官方接口（文档明确写「该接口无需申请权限」）
#        GET https://open.douyin.com/api/douyin/v1/video/get_iframe_by_video?video_id=<id>
#      返回视频标题、宽高，以及官方嵌入播放器地址。
#
#   ② 官方播放器 https://open.douyin.com/player/video?vid=<id>
#      它内部调用的正是 https://www.douyin.com/aweme/v1/web/aweme/detail/
#      ——和 yt-dlp 撞的是同一个风控接口。但实测：
#          不带 Referer          -> 403 Blocked by ArgusSecurityPlugin Uifid Not Found
#          带官方播放器 Referer  -> 200，完整 aweme_detail（含 video.play_addr.url_list）
#      即抖音风控对「自家官方嵌入播放器」开了白名单。
#
#   于是无需 cookie、无需 a_bogus 签名、无需 uifid，就能拿到无水印直链。

DY_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
DY_OFFICIAL_REFERER = "https://open.douyin.com/"
DY_ID_RE = re.compile(r"/(?:video|share/video|note|slides)/(\d{6,})")


def _http(url: str, headers: dict | None = None, timeout: int = 30,
          data: bytes | None = None) -> bytes:
    import urllib.request
    h = {"User-Agent": DY_UA}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def douyin_resolve_id(url: str, quiet: bool = True) -> str:
    """把任意形态的抖音链接解析成 video_id。

    支持 v.douyin.com 短链、www.douyin.com/video/<id>、
    www.iesdouyin.com/share/video/<id>、以及纯数字 id。
    """
    url = (url or "").strip()
    if re.fullmatch(r"\d{6,}", url):
        return url
    m = DY_ID_RE.search(url)
    if m and "douyin.com/video/" in url:
        return m.group(1)

    # 短链：跟一次跳转拿 Location
    import urllib.request
    import urllib.error

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(urllib.request.Request(url, headers={"User-Agent": DY_UA}), timeout=20)
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "")
        m2 = DY_ID_RE.search(loc) or re.search(r"(\d{15,})", loc)
        if m2:
            if not quiet:
                print(f"▶ 短链解析 -> {m2.group(1)}")
            return m2.group(1)
    except Exception:
        pass

    m3 = DY_ID_RE.search(url) or re.search(r"(\d{15,})", url)
    if m3:
        return m3.group(1)
    raise RuntimeError(f"无法从链接里解析出抖音 video_id：{url}")


def douyin_official_detail(video_id: str, quiet: bool = True) -> dict:
    """走官方播放器白名单拿完整 aweme_detail。"""
    api = ("https://open.douyin.com/api/douyin/v1/video/get_iframe_by_video"
           f"?video_id={video_id}")
    title = ""
    try:
        j = json.loads(_http(api, timeout=25).decode("utf-8", "replace"))
        title = ((j.get("data") or {}).get("video_title") or "").strip()
        if not quiet and title:
            print(f"▶ 官方接口标题：{title[:50]}")
    except Exception as e:
        if not quiet:
            print(f"⚠️  官方 iframe 接口未取到标题（{type(e).__name__}），继续走播放器通道")

    q = ("device_platform=webapp&aid=6383&channel=channel_pc_web&pc_client_type=1"
         "&version_code=170400&version_name=17.4.0&cookie_enabled=true"
         "&screen_width=1440&screen_height=900&browser_language=zh-CN"
         "&browser_platform=Win32&browser_name=Chrome&browser_version=131.0.0.0"
         "&browser_online=true&engine_name=Blink&engine_version=131.0.0.0"
         "&os_name=Windows&os_version=10&cpu_core_num=8&device_memory=8&platform=PC"
         "&downlink=10&effective_type=4g&round_trip_time=100"
         f"&aweme_id={video_id}")
    url = "https://www.douyin.com/aweme/v1/web/aweme/detail/?" + q
    raw = _http(url, headers={
        "Referer": DY_OFFICIAL_REFERER,
        "Origin": "https://open.douyin.com",
        "Accept": "application/json, text/plain, */*",
    }, timeout=30)
    data = json.loads(raw.decode("utf-8", "replace"))
    detail = data.get("aweme_detail")
    if not detail:
        raise RuntimeError(f"官方播放器通道没返回视频详情：{raw[:200]!r}")
    if title and not detail.get("desc"):
        detail["desc"] = title
    return detail


def douyin_pick_url(detail: dict) -> tuple[str, str]:
    """从 aweme_detail 里挑最合适的播放直链（优先无水印、优先码率高的 H264）。"""
    v = detail.get("video") or {}

    def first(d):
        if isinstance(d, dict):
            u = d.get("url_list") or []
            for x in u:
                if x:
                    return x
        return ""

    # 优先码率表里最高档的 H264（兼容性最好）
    brs = [b for b in (v.get("bit_rate") or []) if isinstance(b, dict)]
    brs.sort(key=lambda b: b.get("bit_rate") or 0, reverse=True)
    for b in brs:
        u = first(b.get("play_addr"))
        if u:
            return u, f"{b.get('gear_name') or 'bitrate'}"

    for key in ("play_addr_h264", "play_addr", "play_addr_265", "download_addr"):
        u = first(v.get(key))
        if u:
            return u, key
    raise RuntimeError("aweme_detail 里没有可用的播放直链")


def douyin_fetch(url: str, workdir: Path, cfg: dict,
                 quiet: bool = True) -> tuple[Path, dict]:
    """抖音官方通道：解析 id -> 官方详情 -> 下载媒体。"""
    vid = douyin_resolve_id(url, quiet)
    if not quiet:
        print(f"▶ video_id {vid}")
    detail = douyin_official_detail(vid, quiet)
    play, tag = douyin_pick_url(detail)
    v = detail.get("video") or {}
    if not quiet:
        print(f"▶ 选中档位 {tag}｜{v.get('width')}x{v.get('height')}")

    ext = ".mp4"
    dst = workdir / f"douyin{ext}"
    # CDN 需要 Referer/UA，否则 403
    body = _http(play, headers={
        "Referer": "https://www.douyin.com/",
        "Accept": "*/*",
        "Range": "bytes=0-",
    }, timeout=300)
    dst.write_bytes(body)

    info = {
        "title": detail.get("desc") or "",
        "uploader": ((detail.get("author") or {}).get("nickname") or ""),
        "duration": (v.get("duration") or 0) / 1000.0 or None,
        "webpage_url": f"https://www.douyin.com/video/{vid}",
        "id": vid,
        "width": v.get("width"),
        "height": v.get("height"),
    }
    return dst, info


# ---------------------------------------------------------------- 下载

def run(cmd: list[str], desc: str, quiet: bool = False) -> subprocess.CompletedProcess:
    if not quiet:
        print(f"▶ {desc}")
    p = subprocess.run(
        cmd, capture_output=True, text=True, errors="replace",
        env=spawn_env(),
    )
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-12:]
        raise RuntimeError(f"{desc} 失败（退出码 {p.returncode}）：\n" + "\n".join(tail))
    return p


def download_audio(url: str, platform: str, workdir: Path, cfg: dict,
                   quiet: bool) -> tuple[Path, dict]:
    """yt-dlp 下载最佳音频轨，返回 (音频文件, 元信息)。"""
    tmpl = str(workdir / "src.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "--no-warnings",
        # 优先「有音轨」的格式：转写只要声音，而且能避开纯视频的 DASH 分片。
        # 小红书实测：EF5 的 .m4s 是 video-only 分片（无 acodec），单独下载后
        # ffmpeg 会报 "Output file does not contain any stream"；EF4 的 .mp4 才带 aac。
        "-f", "bestaudio/best[acodec!=none]/best[ext=mp4]/best",
        "--write-info-json",
        "-o", tmpl,
    ]

    # cookie：抖音和小红书都要求「新鲜 cookie」，否则拿不到媒体地址。
    # 优先用显式配置的 cookies.txt；没配就尝试从内置浏览器自动导出。
    cookie_path = cfg.get("cookies_file")
    if cookie_path:
        cmd += ["--cookies", os.path.expanduser(cookie_path)]
    else:
        auto = workdir / "cookies.txt"
        try:
            n = export_cookies(platform, auto, quiet)
        except Exception:
            n = 0
        if n:
            if not quiet:
                print(f"▶ 已从内置浏览器导出 {n} 条 {platform} cookie")
            cmd += ["--cookies", str(auto)]
        elif not quiet:
            print(f"⚠️  没找到 {platform} cookie —— 若抓取失败，"
                  f"请先在 DSH 内置浏览器里登录一次该平台")
    if cfg.get("proxy"):
        cmd += ["--proxy", cfg["proxy"]]
    cmd += list(cfg.get("ytdlp_extra_args") or [])
    cmd.append(url)

    run(cmd, "yt-dlp 下载音频", quiet)

    info_files = list(workdir.glob("src.info.json"))
    info = {}
    if info_files:
        try:
            info = json.loads(info_files[0].read_text(encoding="utf-8"))
        except Exception:
            info = {}

    media = [p for p in workdir.glob("src.*")
             if p.suffix.lower() not in (".json", ".part", ".ytdl")]
    if not media:
        raise RuntimeError("yt-dlp 跑完了但没找到音频文件")
    media.sort(key=lambda p: p.stat().st_size, reverse=True)
    return media[0], info


def to_pcm16k(src: Path, workdir: Path, quiet: bool, url: str = "") -> Path:
    """转成 16kHz 单声道 PCM WAV —— sherpa-onnx 与多数云端 ASR 都吃这个。"""
    dst = workdir / "audio16k.wav"
    try:
        run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(dst)],
            "ffmpeg 转 16kHz 单声道",
            quiet,
        )
    except RuntimeError as e:
        if "does not contain any stream" not in str(e):
            raise
        # 下到的是无音轨的纯视频分片 —— 丢掉重取一个有音轨的
        if not quiet:
            print("⚠️  该格式无音轨，改取带音轨的格式重试")
        src.unlink(missing_ok=True)
        if not url:
            raise RuntimeError("该格式无音轨，且没有原始链接可供重取") from e
        retry_tmpl = str(workdir / "retry.%(ext)s")
        run(
            [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings",
             "-f", "best[acodec!=none][vcodec!=none]/best[acodec!=none]/best",
             "-o", retry_tmpl, url],
            "yt-dlp 重取带音轨格式",
            quiet,
        )
        cands = sorted([p for p in workdir.glob("retry.*")
                        if p.suffix.lower() not in (".json", ".part", ".ytdl")],
                       key=lambda p: p.stat().st_size, reverse=True)
        if not cands:
            raise RuntimeError("重取失败：没拿到任何媒体文件")
        run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(cands[0]), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(dst)],
            "ffmpeg 转 16kHz 单声道（重试）",
            quiet,
        )

    if not dst.exists() or dst.stat().st_size < 1024:
        raise RuntimeError("转码结果异常（文件为空）")
    return dst


def read_wav(path: Path):
    import wave
    import numpy as np

    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise RuntimeError(f"期望 16bit PCM，实际 {sw * 8}bit")
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


# ---------------------------------------------------------------- ASR：本地

def find_models(model_dir: Path) -> tuple[Path, Path, Path]:
    d = model_dir.expanduser()
    if not d.is_dir():
        raise RuntimeError(f"模型目录不存在：{d}\n先运行：vtc --fetch-models")

    cands = sorted(d.glob("sherpa-onnx-sense-voice-*"))
    if not cands:
        raise RuntimeError(f"{d} 下没找到 SenseVoice 模型目录\n先运行：vtc --fetch-models")

    mdir = cands[0]
    model = None
    for name in ("model.int8.onnx", "model.onnx", "model.fp16.onnx"):
        if (mdir / name).exists():
            model = mdir / name
            break
    if model is None:
        onnx = list(mdir.glob("*.onnx"))
        if not onnx:
            raise RuntimeError(f"{mdir} 里没有 .onnx 模型")
        model = onnx[0]

    tokens = mdir / "tokens.txt"
    if not tokens.exists():
        raise RuntimeError(f"缺少 tokens.txt：{mdir}")

    vad = None
    for name in ("silero_vad.onnx", "silero_vad_v5.onnx", "silero_vad_v4.onnx"):
        if (d / name).exists():
            vad = d / name
            break
    return model, tokens, vad


def transcribe_local(wav: Path, cfg: dict, progress=print):
    """sherpa-onnx SenseVoice 离线识别，用 Silero VAD 切句。

    返回 [(起始秒, 文本), ...]
    """
    import numpy as np
    import sherpa_onnx

    lc = cfg["asr"]["local"]
    model, tokens, vad_path = find_models(Path(cfg["model_dir"]))
    nthreads = int(lc.get("num_threads") or 4)

    progress(f"▶ 加载 SenseVoice 模型（{model.name}，{nthreads} 线程）")
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model),
        tokens=str(tokens),
        num_threads=nthreads,
        use_itn=bool(lc.get("use_itn", True)),
        language=str(lc.get("language") or "zh"),
        debug=False,
    )

    samples, sr = read_wav(wav)
    dur = len(samples) / sr
    progress(f"▶ 音频时长 {fmt_dur(dur)}，开始识别")

    results: list[tuple[float, str]] = []

    def decode(sample_slice, start_sample: int):
        st = recognizer.create_stream()
        st.accept_waveform(sr, sample_slice)
        recognizer.decode_stream(st)
        txt = (st.result.text or "").strip()
        if txt:
            results.append((start_sample / sr, txt))

    if vad_path is None:
        # 无 VAD：整段喂进去（SenseVoice 对 30s 内最稳，长音频会有截断风险）
        progress("⚠️  未找到 VAD 模型，整段识别（长视频可能截断）")
        decode(samples, 0)
    else:
        vc = sherpa_onnx.VadModelConfig()
        vc.silero_vad.model = str(vad_path)
        vc.silero_vad.threshold = 0.5
        vc.silero_vad.min_silence_duration = 0.25
        vc.silero_vad.min_speech_duration = 0.25
        vc.silero_vad.max_speech_duration = 20
        vc.sample_rate = sr
        vad = sherpa_onnx.VoiceActivityDetector(vc, buffer_size_in_seconds=60)

        win = 512
        n = len(samples)
        for i in range(0, n, win):
            vad.accept_waveform(samples[i:i + win])
            while not vad.empty():
                seg = vad.front
                decode(seg.samples, int(getattr(seg, "start", 0)))
                vad.pop()
            if i and i % (win * 400) == 0:
                pct = 100.0 * i / n
                progress(f"    … {pct:5.1f}%")
        vad.flush()
        while not vad.empty():
            seg = vad.front
            decode(seg.samples, int(getattr(seg, "start", 0)))
            vad.pop()

    progress(f"▶ 识别完成，共 {len(results)} 段")
    return results


# ---------------------------------------------------------------- 错误翻译

BLOCKED_HOST_HELP = """\
网络层就被掐断了，不是本工具的 bug。

现象：对 {host} 的 TLS 握手在 Server Hello 前被重置
      （SSL: UNEXPECTED_EOF_WHILE_READING）。

本机实测：设备在走代理（域名解析到 198.18.x.x，Clash 类 fake-IP 段），
www.douyin.com 大概率被路由到了境外节点，而抖音会重置境外 IP。
同一台机器上 v.douyin.com / www.iesdouyin.com / 小红书各域名都正常。

修法（需要你在代理 App 里操作，脚本改不了）：
    rules:
      - DOMAIN-SUFFIX,douyin.com,DIRECT
      - DOMAIN-SUFFIX,iesdouyin.com,DIRECT

临时替代方案：在抖音 App 里把视频「保存到相册」或录屏，
然后直接转本地文件：
    vtc /storage/emulated/0/DCIM/Camera/xxx.mp4
"""


def friendly_error(err: Exception, platform: str = "") -> str:
    msg = str(err)
    if "UNEXPECTED_EOF_WHILE_READING" in msg or "SSL routines" in msg:
        return BLOCKED_HOST_HELP.format(host="www.douyin.com")
    if "Fresh cookies" in msg or "No video formats found" in msg:
        who = {"douyin": "抖音", "xhs": "小红书"}.get(platform, "该平台")
        return (
            f"{who}要求「新鲜 cookie」才能取到媒体地址（平台反爬）。\n\n"
            "处理步骤：\n"
            f"  1. 在 DSH 内置浏览器里打开并登录 "
            f"{'www.douyin.com' if platform == 'douyin' else 'www.xiaohongshu.com'}\n"
            "  2. ⚠️ 关掉那个标签页（cookie 这时才刷到磁盘）\n"
            "  3. 运行：vtc --export-cookies "
            f"{platform or 'all'}\n"
            "  4. 重新跑本命令\n\n"
            "（vtc 每次运行会自动尝试导出并使用 cookie，导出成功会打印条数）"
        )
    return msg


# ---------------------------------------------------------------- ASR：云端

def transcribe_cloud(wav: Path, cfg: dict, progress=print):
    """OpenAI 兼容的 /audio/transcriptions 接口（SiliconFlow / DashScope 兼容模式等）。"""
    import urllib.request
    import uuid

    cc = cfg["asr"]["cloud"]
    key = cc.get("api_key") or ""
    if not key and cc.get("api_key_env"):
        key = os.environ.get(cc["api_key_env"], "")
    if not key:
        raise RuntimeError(
            "云端 ASR 没配 API Key。\n"
            f"  填到 config.json 的 asr.cloud.api_key，或设环境变量 {cc.get('api_key_env')}"
        )

    url = cc["base_url"].rstrip("/") + "/audio/transcriptions"
    boundary = "----vtc" + uuid.uuid4().hex
    data = wav.read_bytes()

    def field(name, value):
        return (f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{name}"\r\n\r\n{value}\r\n').encode()

    body = b""
    body += field("model", cc["model"])
    body += (f"--{boundary}\r\nContent-Disposition: form-data; "
             f'name="file"; filename="{wav.name}"\r\n'
             f"Content-Type: audio/wav\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    progress(f"▶ 上传到云端 ASR：{url}")
    try:
        with urllib.request.urlopen(req, timeout=int(cc.get("timeout", 300))) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"云端 ASR 返回 {e.code}：{detail}")

    text = payload.get("text") or payload.get("transcript") or ""
    if not text:
        raise RuntimeError(f"云端返回里没有文本字段：{json.dumps(payload, ensure_ascii=False)[:300]}")
    return [(0.0, text.strip())]


# ---------------------------------------------------------------- 输出

def fmt_dur(sec) -> str:
    try:
        sec = int(float(sec))
    except Exception:
        return "?"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def para_graph(text: str, width: int = 240) -> str:
    """按句末标点切段，避免糊成一大坨。"""
    parts = re.split(r"(?<=[。！？!?…])", text)
    out, buf = [], ""
    for p in parts:
        if not p:
            continue
        buf += p
        if len(buf) >= width:
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return "\n\n".join(out) if out else text


def yaml_str(s) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def safe_name(s: str, limit: int = 60) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:limit].strip() or "transcript")


def build_markdown(meta: dict, segs, timestamps: bool, backend: str) -> str:
    text = "".join(t for _, t in segs).strip()
    title = meta.get("title") or "未命名视频"
    dur = meta.get("duration")
    tags = ["视频转写", meta.get("platform", "")]

    L = ["---"]
    L.append(f"title: {yaml_str(title)}")
    L.append(f"source: {yaml_str(meta.get('url', ''))}")
    L.append(f"platform: {meta.get('platform', '')}")
    if meta.get("uploader"):
        L.append(f"author: {yaml_str(meta['uploader'])}")
    if dur:
        L.append(f"duration: {fmt_dur(dur)}")
    L.append(f"transcribed_at: {yaml_str(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}")
    L.append(f"asr: {yaml_str(backend)}")
    L.append("tags:")
    for t in tags:
        if t:
            L.append(f"  - {t}")
    L.append("---")
    L.append("")
    L.append(f"# {title}")
    L.append("")
    L.append(f"> **来源**：{meta.get('url', '')}  ")
    if meta.get("uploader"):
        L.append(f"> **作者**：{meta['uploader']}  ")
    if dur:
        L.append(f"> **时长**：{fmt_dur(dur)}  ")
    L.append(f"> **转写**：{backend}")
    L.append("")
    L.append("## 文案")
    L.append("")
    L.append(para_graph(text))
    L.append("")

    if timestamps:
        L.append("## 带时间轴")
        L.append("")
        for start, t in segs:
            L.append(f"- `[{fmt_dur(start)}]` {t}")
        L.append("")

    stats = f"共 {len(text)} 字"
    if dur:
        stats += f" / 音频 {fmt_dur(dur)}"
    L.append("---")
    L.append("")
    L.append(f"*{stats}；由 vtc 本机转写*")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- 模型下载

MODEL_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
MODEL_TAR = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"


def fetch_models(model_dir: Path) -> None:
    import tarfile
    import urllib.request

    d = model_dir.expanduser()
    d.mkdir(parents=True, exist_ok=True)

    todo = [(f"{MODEL_BASE}/{MODEL_TAR}", MODEL_TAR, True),
            (f"{MODEL_BASE}/silero_vad.onnx", "silero_vad.onnx", False)]

    for url, name, unpack in todo:
        dst = d / name
        if unpack and list(d.glob("sherpa-onnx-sense-voice-*")):
            print(f"✅ 模型已存在，跳过 {name}")
            continue
        if not unpack and dst.exists() and dst.stat().st_size > 100_000:
            print(f"✅ 已存在，跳过 {name}")
            continue

        print(f"⬇ 下载 {name}")
        tmp = dst.with_suffix(dst.suffix + ".part")

        def hook(blocks, bs, total):
            done = blocks * bs
            if total > 0:
                pct = min(100.0, 100.0 * done / total)
                sys.stdout.write(f"\r   {pct:5.1f}%  {done/1048576:6.1f}/{total/1048576:.1f} MB")
                sys.stdout.flush()

        urllib.request.urlretrieve(url, tmp, hook)
        print()
        tmp.replace(dst)

        if unpack:
            print(f"   解包 {name} …")
            with tarfile.open(dst, "r:bz2") as tf:
                tf.extractall(d)
            dst.unlink(missing_ok=True)

    print(f"✅ 模型就绪：{d}")
    for p in sorted(d.iterdir()):
        print("   ", p.name)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ensure_native_libs()
    dns_ok = install_dns_bypass()

    ap = argparse.ArgumentParser(
        prog="vtc",
        description="抖音/小红书视频 -> Markdown 文字稿（本机转写，无需浏览器扩展）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  vtc "https://v.douyin.com/xxxx/"
  vtc "3.14 复制打开抖音，看看【作者的作品】 https://v.douyin.com/xxxx/"
  vtc --timestamps -o ~/notes "https://www.xiaohongshu.com/explore/xxxx"
  vtc --backend cloud "https://..."
  vtc ~/Movies/某个视频.mp4        # 也可以直接转本地音视频
  vtc --fetch-models
  vtc --export-cookies all
""",
    )
    ap.add_argument("input", nargs="?", help="视频链接，或 App 分享出来的整段文案")
    ap.add_argument("-o", "--out", help="输出目录（默认取 config.json）")
    ap.add_argument("--backend", choices=["local", "cloud"], help="ASR 后端")
    ap.add_argument("--timestamps", action="store_true", help="额外输出带时间轴逐句版")
    ap.add_argument("--keep-audio", action="store_true", help="保留中间音频文件")
    ap.add_argument("--fetch-models", action="store_true", help="下载/检查本地 ASR 模型")
    ap.add_argument("--export-cookies", metavar="PLATFORM",
                    choices=["douyin", "xhs", "all"],
                    help="从 DSH 内置浏览器导出 cookie（先在浏览器里登录该平台）")
    ap.add_argument("--print-config", action="store_true", help="打印当前配置")
    ap.add_argument("-q", "--quiet", action="store_true", help="安静模式")
    args = ap.parse_args()

    cfg = load_config()

    if args.print_config:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0

    if args.fetch_models:
        fetch_models(Path(cfg["model_dir"]))
        return 0

    if args.export_cookies:
        plats = ["douyin", "xhs"] if args.export_cookies == "all" else [args.export_cookies]
        total = 0
        for p in plats:
            out = HERE / f"cookies-{p}.txt"
            n = export_cookies(p, out)
            total += n
            if n:
                print(f"✅ {p}：导出 {n} 条 -> {out}")
            else:
                print(f"⚠️  {p}：没找到 cookie。请先在 DSH 内置浏览器里打开并登录 "
                      f"{'www.douyin.com' if p == 'douyin' else 'www.xiaohongshu.com'}，再执行本命令。")
        if total:
            print("\n把 cookies_file 指向该文件即可（config.json），或不配置——vtc 每次会自动导出。")
        return 0 if total else 1

    if not args.input:
        ap.print_help()
        return 2

    def say(*a):
        if not args.quiet:
            print(*a)

    t0 = time.time()
    backend_name = args.backend or cfg["asr"]["backend"]
    platform = ""

    try:
        kind, value, platform = resolve_input(args.input)

        workdir = Path(tempfile.mkdtemp(prefix="vtc-"))
        try:
            if kind == "file":
                media = value
                info = {"title": media.stem, "duration": None}
                platform = "local"
                say(f"▶ 本地文件 {media.name}（{media.stat().st_size/1048576:.1f} MB）")
            else:
                url = value
                say(f"▶ 平台 {platform}｜链接 {url}")
                if platform == "douyin":
                    # 官方通道：无需 cookie / 签名 / uifid
                    try:
                        media, info = douyin_fetch(url, workdir, cfg, args.quiet)
                        say(f"▶ 已下载 {media.name}"
                            f"（{media.stat().st_size/1048576:.1f} MB）")
                    except Exception as dy_err:
                        say(f"⚠️  官方通道失败（{dy_err}），退回 yt-dlp")
                        media, info = download_audio(url, platform, workdir,
                                                     cfg, args.quiet)
                        say(f"▶ 已下载 {media.name}"
                            f"（{media.stat().st_size/1048576:.1f} MB）")
                else:
                    media, info = download_audio(url, platform, workdir, cfg, args.quiet)
                    say(f"▶ 已下载 {media.name}"
                        f"（{media.stat().st_size/1048576:.1f} MB）")

            wav = to_pcm16k(media, workdir, args.quiet, url if kind == "url" else "")

            if backend_name == "cloud":
                segs = transcribe_cloud(wav, cfg, say)
                backend_label = f"云端 {cfg['asr']['cloud']['model']}"
            else:
                segs = transcribe_local(wav, cfg, say)
                backend_label = "本机 sherpa-onnx SenseVoice"

            if not segs or not "".join(t for _, t in segs).strip():
                raise RuntimeError("识别结果为空——可能是纯音乐/无人声，或模型未正确加载")

            meta = {
                "title": info.get("title") or info.get("desc") or "",
                "uploader": info.get("uploader") or info.get("creator") or info.get("nickname") or "",
                "duration": info.get("duration"),
                "url": info.get("webpage_url") or (str(value) if kind == "file" else value),
                "platform": platform,
            }
            if not meta["title"]:
                meta["title"] = f"{platform} 视频 {datetime.now().strftime('%Y%m%d-%H%M')}"

            md = build_markdown(meta, segs, args.timestamps, backend_label)

            outdir = Path(os.path.expanduser(args.out or cfg["output_dir"]))
            outdir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            outfile = outdir / f"{stamp} {safe_name(meta['title'])}.md"
            outfile.write_text(md, encoding="utf-8")

            if args.keep_audio or cfg.get("keep_audio"):
                keep = outdir / f"{stamp} {safe_name(meta['title'])}.wav"
                shutil.copy2(wav, keep)
                say(f"▶ 音频已保留：{keep}")

            say("")
            say(f"✅ 完成，用时 {time.time()-t0:.0f}s")
            say(f"📄 {outfile}")
            say(f"   正文 {len(''.join(t for _,t in segs))} 字 / {len(segs)} 段")
            return 0
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"\n❌ {friendly_error(e, platform)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
