"""
Eplus DRM Downloader — CLI 完整下载管道
(核心逻辑与结构由 eplus-drm-keys/cli.ts + lib/ 迁移而来)

模式从 MPD URL 自动检测:
  vod.live.eplus.jp     → VOD (回放/存档)
  stream.live.eplus.jp  → LIVE (直播)
可通过 --mode vod|live 覆盖。

用法:
  uv run main.py                          # 完整流程: 提取 key → 下载
  uv run main.py --keys-only              # 只提取 key, 跳过下载
  uv run main.py --mode live              # 强制 LIVE 模式
  uv run main.py --playerUrl=... --cookieStr=...
  uv run main.py --urlMpd=... --cookieStr=... --authUrl=...

只填 PLAYER_URL + 会话 Cookie 即可: 脚本会从播放页读取标题、MPD 地址、
channel_id、一次性 sid 和 CloudFront 签名 Cookie。

默认读取 .env 文件 (python-dotenv, 覆盖已存在的环境变量)。CLI 参数优先于 .env 值。
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TypedDict

import requests
from dotenv import load_dotenv
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

# ── 日志: [YYYY-MM-DD HH:MM:SS] 消息 (与 cli.ts 的 log() 一致) ────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("eplus-drm")

# Windows 管道/重定向下 stdout 可能是 cp1252, 强制 UTF-8 输出,
# 避免打印 emoji/中文时抛 UnicodeEncodeError (与 cli.ts 的 UTF-8 输出一致)
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[attr-defined]


# ── 应用目录: 打包 exe 时取 exe 所在目录, 开发时取脚本目录 ──────
# 关键坑: Nuitka onefile 下 __file__ 指向临时解压目录, sys.executable 指向
# 临时解压的 python.exe, 而且 sys.frozen 根本不存在 —— 三者都不是 exe 本体。
# 因此要么用 GetModuleFileNameW 取绝对 exe 路径, 要么退回 sys.argv[0]。
def _app_dir() -> Path:
    # Nuitka 会注入 __compiled__ (模块), 其 .onefile 字段标识 onefile 模式。
    compiled = globals().get("__compiled__")
    if compiled is not None and getattr(compiled, "onefile", False):
        return _exe_dir()
    if getattr(sys, "frozen", False):  # PyInstaller 等 frozen 应用
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _exe_dir() -> Path:
    """Nuitka onefile 下定位真实 exe 所在目录。"""
    if os.name == "nt":
        with contextlib.suppress(Exception):
            import ctypes
            from ctypes import wintypes

            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            ctypes.windll.kernel32.GetModuleFileNameW(None, buf, wintypes.MAX_PATH)
            if buf.value:
                return Path(buf.value).resolve().parent
    # 兜底: onefile 模式下 sys.argv[0] 就是 exe 路径 (绝对或相对 cwd)。
    return Path(sys.argv[0]).resolve().parent


APP_DIR = _app_dir()

# 优先读 exe/脚本旁的 .env; 找不到再退回 cwd 下的 .env
# override=True: 让 .env 覆盖父进程里可能残留的旧 URL_MPD / COOKIE_MPD
# load_dotenv 返回是否成功加载到某个 .env 文件, 用于给出友好提示
env_found = load_dotenv(APP_DIR / ".env", override=True)
if not os.getenv("URL_MPD") and not os.getenv("PLAYER_URL"):
    env_found = load_dotenv(override=True) or env_found


def app_path(p: str) -> Path:
    """把相对路径锚定到应用目录 (其次 cwd), 绝对路径原样返回。"""
    path = Path(p)
    if path.is_absolute():
        return path
    anchored = (APP_DIR / path).resolve()
    if anchored.exists():
        return anchored
    from_cwd = (Path.cwd() / path).resolve()
    if from_cwd.exists():
        return from_cwd
    return anchored


# ── 常量 ──────────────────────────────────────────────────────
LICENSE_URL = "https://lic.drmtoday.com/license-proxy-widevine/cenc/?specConform=true"

MPD_KID_RE = re.compile(r'cenc:default_KID="(?P<kid>[^"]+)"')
MPD_BASE_RE = re.compile(r"https://(?:vod|stream)\.live\.eplus\.jp/out/v1/(?P<base>[^/]*)/")
MPD_PATH_BASE_RE = re.compile(r"/out/v1/([^/]+)/")
AUTH_SID_RE = re.compile(r"(?P<sep>[?&])sid=[^&]*")
PLAYER_APP_RE = re.compile(r"var\s+app\s*=\s*(\{.*?\})\s*;\s*</script>", re.S)
PLAYER_APP_ID_RE = re.compile(r'"app_id"\s*:\s*"([^"]+)"')
PLAYER_ASSET_ID_RE = re.compile(r'"assetId"\s*:\s*"([^"]+)"')
PLAYER_SESSION_RE = re.compile(r"var\s+streamSession\s*=\s*['\"]([^'\"]+)['\"]")
PLAYER_MPD_RE = re.compile(r'var\s+channels_dash\s*=\s*\["([^"]+)"\]')
APP_NAME_RE = re.compile(r'"app_name"\s*:\s*"((?:[^"\\]|\\.)*)"')
PAGE_TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.I)
# 标题里不允许出现在文件名中的字符 (Windows/macOS 通用)
UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)

# --verbose / DEBUG=true 时打印完整响应体
VERBOSE = False


class KeysResult(TypedDict):
    """get_eplus_keys 的返回结构 (与 lib/eplus.ts 一致)。"""

    mpdUrl: str
    base: str | None
    kid: str
    pssh: str
    keys: list[str]
    keyContainers: list[dict[str, str]]
    selectedKey: str
    title: str | None
    cookieStr: str


class PlayerSession(TypedDict, total=False):
    """播放页解析结果 (对应 lib/eplus.ts::PlayerSession)。"""

    title: str
    sid: str
    appId: str
    assetId: str
    mpdUrl: str
    cookies: str
    cookiesExpireAt: float


class DownloadOpts(TypedDict):
    """N_m3u8DL-RE 下载选项。"""

    urlMpd: str
    cookieStr: str
    key: str
    downloaderPath: str
    ffmpegPath: str
    shakaPackagerPath: str
    outputDir: str
    tempDir: str
    logFilePath: str
    threadCount: int
    recordLimit: str | None
    waitTime: int | None
    title: str | None


# ── CLI 参数解析 (--key=value 与 --flag) ───────────────────────
def parse_args() -> dict[str, str]:
    """简易 argv 解析, 与 cli.ts 保持一致:
    --urlMpd=x → {'urlMpd': 'x'}, --keys-only → {'keys-only': 'true'}
    """
    args: dict[str, str] = {}
    for arg in sys.argv[1:]:
        if arg.startswith("--"):
            eq_idx = arg.find("=")
            if eq_idx > 2:
                args[arg[2:eq_idx]] = arg[eq_idx + 1 :]
            else:
                args[arg[2:]] = "true"
    return args


# ── 基础工具 (对应 lib/parse-cookies.ts) ───────────────────────
def ensure_dir(dir_path: str) -> None:
    """确保目录存在 (不存在则递归创建)。"""
    app_path(dir_path).mkdir(parents=True, exist_ok=True)


def parse_cookies(cookie_str: str) -> dict[str, str]:
    """把 Cookie 字符串解析为 key-value 字典。"""
    cookies: dict[str, str] = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            cookies[k] = v
    return cookies


def serialize_cookies(cookies: dict[str, str]) -> str:
    """把字典拼回 Cookie 头。"""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def merge_cookies(base: str, updates: str | None) -> str:
    """把 updates 叠加到 base 上, 保留其余 Cookie (对应 parse-cookies.ts::mergeCookies)。"""
    if not updates:
        return base
    return serialize_cookies({**parse_cookies(base), **parse_cookies(updates)})


# ── 诊断输出 (对应 lib/eplus.ts 的 preview / maskSecrets) ───────
SECRET_RE = re.compile(
    r'("(?:auth_token|token|license|session_?id|user_?id)"\s*:\s*")([^"]{4})[^"]*', re.I
)


def preview(text: str, max_len: int | None = None) -> str:
    """截断并打码后的响应体预览, 用于日志。"""
    limit = max_len if max_len is not None else (4000 if VERBOSE else 600)
    masked = SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}…", text)
    shown = masked[:limit] + "…" if len(masked) > limit else masked
    return f"{shown.strip()} [{len(text)} chars]"


def _set_cookie_headers(res: requests.Response) -> list[str]:
    """取出全部 Set-Cookie (requests 的 headers 会合并同名项, 必须走 raw)。"""
    with contextlib.suppress(Exception):
        values = res.raw.headers.getlist("Set-Cookie")
        if values:
            return list(values)
    return [v for k, v in res.raw.headers.items() if k.lower() == "set-cookie"]


def _http_date_to_ms(value: str | None) -> float | None:
    if not value:
        return None
    with contextlib.suppress(Exception):
        return parsedate_to_datetime(value).timestamp() * 1000
    return None


def _policy_info(value: str) -> tuple[str, float | None]:
    """解码 CloudFront-Policy (base64url), 返回 (Resource, 过期毫秒时间戳)。"""
    try:
        # CloudFront 用 URL-safe base64 ('-'/'_'), 需先还原成标准字母表
        normalized = value.replace("-", "+").replace("_", "/")
        padded = normalized + "=" * (-len(normalized) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", "replace")
        payload = json.loads(decoded[decoded.find("{") : decoded.rfind("}") + 1])
        statement = payload["Statement"][0]
        epoch = statement.get("Condition", {}).get("DateLessThan", {}).get("AWS:EpochTime")
        return statement.get("Resource", ""), (float(epoch) * 1000 if epoch else None)
    except Exception:
        return "", None


def pick_cloudfront_cookies(
    set_cookies: list[str], mpd_url: str | None
) -> tuple[str | None, float | None]:
    """挑出签名策略覆盖 mpd_url 的那一组 CloudFront Cookie。"""
    base_match = MPD_PATH_BASE_RE.search(mpd_url) if mpd_url else None
    base = base_match.group(1) if base_match else None

    groups: list[dict[str, object]] = []
    for raw in set_cookies:
        pair = raw.split(";")[0]
        name = pair.split("=", 1)[0]
        if not name.startswith("CloudFront-"):
            continue
        exp_match = re.search(r"expires=([^;]+)", raw, re.I)
        expires = _http_date_to_ms(exp_match.group(1)) if exp_match else None
        if name == "CloudFront-Policy":
            resource, policy_expiry = _policy_info(pair.split("=", 1)[1])
            groups.append(
                {"pairs": [pair], "resource": resource, "expires": policy_expiry or expires}
            )
        elif groups:
            pairs = groups[-1]["pairs"]
            assert isinstance(pairs, list)
            if not any(p.startswith(f"{name}=") for p in pairs):
                pairs.append(pair)

    if not groups:
        return None, None

    chosen = next((g for g in groups if base and base in str(g["resource"])), groups[0])
    pairs = chosen["pairs"]
    assert isinstance(pairs, list)
    cookies = "; ".join(pairs)
    # 响应里轮换过的 ci_session 等也一并带上
    others = [c.split(";")[0] for c in set_cookies if not c.startswith("CloudFront-")]
    if others:
        cookies += "; " + "; ".join(others)
    expires = chosen.get("expires")
    return cookies, expires if isinstance(expires, float) else None


def with_sid(auth_url: str, sid: str) -> str:
    """替换 (或追加) AUTH_URL 的 sid 参数。"""
    if AUTH_SID_RE.search(auth_url):
        return AUTH_SID_RE.sub(lambda m: f"{m.group('sep')}sid={sid}", auth_url)
    sep = "&" if "?" in auth_url else "?"
    return f"{auth_url}{sep}sid={sid}"


def fetch_player_session(player_url: str, cookie_str: str) -> PlayerSession:
    """
    从播放页解析完整会话 (对应 lib/eplus.ts::fetchPlayerSession)。

    一次请求即可拿到: 直播标题、一次性 streamSession(sid)、MPD 地址、
    channel_id (drmEncryptKey.assetId) 以及约 1 小时有效期的 CloudFront 签名 Cookie。

    失败时返回空字典, 调用方回退到 .env 里的值。
    """
    session: PlayerSession = {}
    try:
        res = requests.get(
            player_url,
            headers={
                "User-Agent": USER_AGENT,
                "Cookie": cookie_str,
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=30,
        )
        html = res.text
        log.info(f"[player] GET {player_url} → HTTP {res.status_code} · {len(html)} chars")

        if m := PLAYER_SESSION_RE.search(html):
            session["sid"] = m.group(1)
        if m := PLAYER_APP_ID_RE.search(html):
            session["appId"] = m.group(1)
        if m := PLAYER_ASSET_ID_RE.search(html):
            session["assetId"] = m.group(1)
        if m := PLAYER_MPD_RE.search(html):
            session["mpdUrl"] = m.group(1).replace("\\/", "/")

        title: str | None = None
        if m := PLAYER_APP_RE.search(html):
            with contextlib.suppress(json.JSONDecodeError):
                title = json.loads(m.group(1)).get("app_name")
        if not title and (m := APP_NAME_RE.search(html)):
            with contextlib.suppress(json.JSONDecodeError):
                title = json.loads(f'"{m.group(1)}"')
        if title:
            session["title"] = title

        signed, expires = pick_cloudfront_cookies(_set_cookie_headers(res), session.get("mpdUrl"))
        if signed:
            session["cookies"] = signed
        if expires:
            session["cookiesExpireAt"] = expires

        if title:
            log.info(f"[player] app_name: {title}")
        else:
            page_title = PAGE_TITLE_RE.search(html)
            log.info(
                f"[player] 页面里没有 app_name (页面 <title>: "
                f"{page_title.group(1).strip() if page_title else '(none)'})"
                " — 请检查 PLAYER_URL / Cookie"
            )
        if sid := session.get("sid"):
            log.info(f"[player] streamSession: {sid[:8]}… ({len(sid)} chars)")
        else:
            log.info("[player] 页面里没有 streamSession — 沿用 AUTH_URL 的 sid")
        if mpd := session.get("mpdUrl"):
            log.info(f"[player] MPD: {mpd}")
        if expires:
            log.info(
                f"[player] 签名 Cookie 有效期至 {datetime.fromtimestamp(expires / 1000):%Y-%m-%dT%H:%M:%S}"
            )
        else:
            log.info("[player] 未获取到 CloudFront 签名 Cookie")
    except Exception as e:  # noqa: BLE001 — 播放页只是可选增强
        log.info(f"[player] 请求失败: {e} — 继续使用 .env 中的值")
        return {}
    return session


# ── PSSH 构建 (对应 lib/pssh.ts) ──────────────────────────────
WIDEVINE_SYSTEM_ID = "edef8ba979d64acea3c827dcd51d21ed"
MAGIC_PADDING = b"\x48\xe3\xdc\x95\x9b\x06"  # 原始代码中的 Magic Padding


def create_pssh_from_kid(kid: str) -> str:
    """
    字节级 PSSH 构建逻辑。
    pywidevine 自动生成的 PSSH 某些情况下会导致 400 错误,
    这里强制使用 Widevine SystemID 和原始 padding 逻辑。

    结构: size(4) + "pssh"(4) + version/flags(4) + SystemID(16)
          + data_size(4) + PlayReady 头(2) + KID(16) + padding(6)
    """
    kid = kid.replace("-", "")
    if len(kid) != 32:
        raise AssertionError(f"Wrong KID length: expected 32 hex chars, got {len(kid)}")

    box = bytearray(b"\x00\x00\x008pssh\x00\x00\x00\x00")
    box.extend(bytes.fromhex(WIDEVINE_SYSTEM_ID))
    box.extend(b"\x00\x00\x00\x18\x12\x10")
    box.extend(bytes.fromhex(kid))
    box.extend(MAGIC_PADDING)

    return base64.b64encode(bytes(box)).decode("utf-8")


# ── Key 提取 (对应 lib/eplus.ts) ───────────────────────────────
def extract_kid(mpd_text: str) -> str | None:
    """从 MPD 文本中提取 cenc:default_KID。"""
    m = MPD_KID_RE.search(mpd_text)
    return m.group("kid") if m else None


def extract_base(url: str) -> str | None:
    """从流地址中提取 base 值 (可能为 None, 不影响下载)。"""
    m = MPD_BASE_RE.search(url)
    return m.group("base") if m else None


def get_eplus_keys(
    cookie_str: str,
    wvd_path: str,
    url_mpd: str | None = None,
    auth_url: str | None = None,
    player_url: str | None = None,
) -> KeysResult:
    """
    完整 key 提取流程 (对应 lib/eplus.ts::getEplusKeys):
      0. 请求播放页 → 标题、sid、MPD 地址、channel_id、签名 Cookie
      1. 从 AUTH_URL 获取 auth_token (用新鲜 sid)
      2. 请求 MPD → 提取 KID、base
      3. 由 KID 构建 Widevine PSSH
      4. 加载 WVD 设备, 创建 CDM 会话
      5. 生成 License Challenge → POST 到 License Server
      6. 解析 License 响应 → 提取内容密钥
    url_mpd / auth_url 省略时由 PLAYER_URL 推导。
    """
    # ── Step 0: 播放页 → 标题 / sid / MPD / channel_id / Cookie ─
    player = fetch_player_session(player_url, cookie_str) if player_url else {}
    effective_cookies = merge_cookies(cookie_str, player.get("cookies"))
    cookies = parse_cookies(effective_cookies)
    log.info(f"[cookie] 使用 {len(cookies)} 个 Cookie: {', '.join(cookies) or '(none)'}")

    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": effective_cookies,
    }

    if not url_mpd:
        url_mpd = player.get("mpdUrl")
        if url_mpd:
            log.info("[mpd] 使用 PLAYER_URL 中的 MPD 地址")
    if not url_mpd:
        raise RuntimeError("没有 MPD 地址 — 请设置 URL_MPD 或 PLAYER_URL")

    app_id = player.get("appId")
    asset_id = player.get("assetId")
    fresh_sid = player.get("sid")

    derived_auth_url: str | None = None
    if app_id and asset_id and fresh_sid:
        derived_auth_url = (
            f"https://live.eplus.jp/api/stream/{app_id}/get_auth_token_drm"
            f"?channel_id={asset_id}&sid={fresh_sid}"
        )
    if not auth_url and derived_auth_url:
        auth_url = derived_auth_url
        log.info("[auth] 使用 PLAYER_URL 推导出的 AUTH_URL")
    if not auth_url:
        raise RuntimeError("没有 AUTH_URL — 请设置 AUTH_URL 或 PLAYER_URL")

    # sid 是一次性的: 优先用播放页刚拿到的新 sid, 失败再退回 AUTH_URL 里的
    candidates = [auth_url]
    if fresh_sid:
        refreshed = with_sid(auth_url, fresh_sid)
        if refreshed != auth_url:
            candidates.insert(0, refreshed)
            log.info(f"[auth] 使用 PLAYER_URL 的新 sid {fresh_sid[:8]}…")

    # ── Step 1: 获取 auth_token ─────────────────────────────
    auth_token: str | None = None
    auth_text = ""
    auth_status = 0
    for index, candidate in enumerate(candidates):
        suffix = " (退回 AUTH_URL 的 sid)" if index > 0 else ""
        log.info(f"[auth] GET {candidate}{suffix}")
        auth_res = requests.get(candidate, headers=headers, timeout=30)
        auth_text = auth_res.text
        auth_status = auth_res.status_code
        log.info(
            f"[auth] HTTP {auth_res.status_code} · "
            f"content-type: {auth_res.headers.get('content-type', '(none)')} · "
            f"{len(auth_text)} chars"
        )
        log.info(f"[auth] body: {preview(auth_text)}")
        if auth_res.status_code == 200:
            with contextlib.suppress(json.JSONDecodeError):
                auth_token = json.loads(auth_text).get("auth_token")
            if auth_token:
                break

    if not auth_token:
        hint = ""
        if '"error"' in auth_text:
            hint = (
                '服务端拒绝了请求 ({"error":true})。请设置 PLAYER_URL 以自动换取新 sid，'
                "或在播放器运行时重新复制 AUTH_URL 与 Cookie。"
            )
            if "ci_session" not in cookies:
                hint += (
                    " 当前 Cookie 缺少 ci_session —— 请复制 get_auth_token_drm 请求的"
                    " Cookie 头 (并保留 .mpd 请求里的 CloudFront-*)。"
                )
        raise RuntimeError(
            f"No auth_token in auth response (HTTP {auth_status}) — body: {preview(auth_text)}\n   {hint}"
        )
    log.info(f"[auth] 已获取 auth_token ({len(auth_token)} chars)")

    # ── Step 2: 请求 MPD, 提取 KID / Base ───────────────────
    log.info(f"[mpd] GET {url_mpd}")
    mpd_res = requests.get(url_mpd, headers=headers, timeout=30)
    mpd_text = mpd_res.text
    final_url = mpd_res.url  # 重定向后的最终 URL
    log.info(f"[mpd] HTTP {mpd_res.status_code} · {len(mpd_text)} chars · final URL: {final_url}")
    if mpd_res.status_code != 200:
        raise RuntimeError(f"MPD fetch failed: {mpd_res.status_code} — body: {preview(mpd_text)}")
    if "cenc:default_KID" not in mpd_text:
        raise RuntimeError(f"MPD contains no cenc:default_KID — head: {preview(mpd_text)}")

    base = extract_base(final_url)
    kid = extract_kid(mpd_text)
    if not kid:
        raise RuntimeError("Could not find cenc:default_KID in MPD")
    log.info(f"[mpd] KID={kid} base={base or '(none)'}")

    # ── Step 3: 由 KID 构建 PSSH ────────────────────────────
    pssh = create_pssh_from_kid(kid)
    log.info(f"[pssh] {len(pssh)} chars (base64)")

    # ── Step 4-6: Widevine CDM → License → keys ─────────────
    device = Device.load(wvd_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    log.info("[wvd] 设备已加载, CDM 会话已创建")
    try:
        challenge = cdm.get_license_challenge(session_id, PSSH(pssh))
        log.info(
            f"[license] POST {LICENSE_URL} · challenge {len(challenge)} bytes · "
            f"X-Dt-Auth-Token {len(auth_token)} chars"
        )

        license_res = requests.post(
            LICENSE_URL,
            headers={
                "accept": "*/*",
                "Connection": "keep-alive",
                "X-Dt-Auth-Token": auth_token,
            },
            data=challenge,
            timeout=30,
        )
        log.info(
            f"[license] HTTP {license_res.status_code} · "
            f"content-type: {license_res.headers.get('content-type', '(none)')} · "
            f"{len(license_res.content)} bytes"
        )
        if license_res.status_code != 200:
            raise RuntimeError(
                f"License request failed [{license_res.status_code}]: {preview(license_res.text)}"
            )

        try:
            cdm.parse_license(session_id, license_res.content)
        except Exception as e:
            raise RuntimeError(
                f"Failed to parse license response: {e} — body: {preview(license_res.text)}"
            ) from e
        raw_keys = cdm.get_keys(session_id)
    finally:
        cdm.close(session_id)

    if not raw_keys:
        raise RuntimeError("No valid keys in license response")

    # 与 cli.ts 相同的 key 选择逻辑:
    #   - 单个 key → 使用第一个
    #   - 多个 key → 使用第二个 (index 1)
    key_containers = [{"kid": k.kid.hex, "key": k.key.hex()} for k in raw_keys]
    keys = [f"{k['kid']}:{k['key']}" for k in key_containers]
    selected = key_containers[1] if len(key_containers) > 1 else key_containers[0]
    log.info(f"[license] keys: {', '.join(k['kid'] for k in key_containers)}")

    return {
        "mpdUrl": final_url,
        "base": base,
        "kid": kid,
        "pssh": pssh,
        "keys": keys,
        "keyContainers": key_containers,
        "selectedKey": f"{selected['kid']}:{selected['key']}",
        "title": player.get("title"),
        "cookieStr": effective_cookies,
    }


# ── 下载 (对应 lib/download.ts) ───────────────────────────────
def detect_mode(url: str) -> str:
    """从 MPD URL 检测下载模式 (未知主机默认 VOD, 更安全)。"""
    if "stream.live.eplus.jp" in url:
        return "live"
    if "vod.live.eplus.jp" in url:
        return "vod"
    log.warning(f"⚠️  未知 MPD 主机, 默认按 VOD 处理: {url}")
    return "vod"


def safe_file_name(name: str) -> str:
    """把标题转成安全的文件名前缀 (对应 lib/download.ts::safeFileName)。"""
    cleaned = re.sub(r"\s+", " ", UNSAFE_FILENAME_RE.sub("_", name)).strip().rstrip(". ")
    return cleaned[:80] or "eplus"


def default_save_name(opts: DownloadOpts) -> str:
    """基础输出名: 有标题用标题, 否则 eplus_drm_<时间戳>。"""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    title = opts.get("title")
    return f"{safe_file_name(title)}_{timestamp}" if title else f"eplus_drm_{timestamp}"


def build_common_args(opts: DownloadOpts, save_name: str) -> list[str]:
    """两种模式共用的 N_m3u8DL-RE 参数。"""
    args = [
        opts["urlMpd"],
        "--save-name",
        save_name,
        "--save-dir",
        str(Path(opts["outputDir"]).resolve()),
        "--tmp-dir",
        str(Path(opts["tempDir"]).resolve()),
        "--download-retry-count",
        "5",
        "--auto-select",
        "--thread-count",
        str(opts["threadCount"]),
        "--mp4-real-time-decryption",
        "--decryption-engine",
        "SHAKA_PACKAGER",
        "--decryption-binary-path",
        str(Path(opts["shakaPackagerPath"]).resolve()),
        "--check-segments-count",
        "--ffmpeg-binary-path",
        str(Path(opts["ffmpegPath"]).resolve()),
        "-H",
        f"Cookie: {opts['cookieStr']}",
        "-mt",
        "--del-after-done",
        "--key",
        opts["key"],
        "--log-level",
        "INFO",
    ]
    if opts.get("logFilePath"):
        args += ["--log-file-path", str(Path(opts["logFilePath"]).resolve())]
    return args


def run_downloader(downloader_path: str, args: list[str]) -> None:
    """同步运行 N_m3u8DL-RE, 非零退出码视为失败。"""
    proc = subprocess.run([downloader_path, *args], check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"N_m3u8DL-RE exited with code {proc.returncode}")


def download_vod(opts: DownloadOpts) -> None:
    """
    VOD (回放/存档) 下载。
    输出: <标题>_<时间戳>.mp4

    --live-perform-as-vod  确保所有分片收集完毕再 "done"
    --mux-after-done mp4   得到带 moov atom 的标准 MP4
    """
    save_name = default_save_name(opts)
    args = build_common_args(opts, save_name)
    args += ["--live-perform-as-vod", "--mux-after-done", "format=mp4"]

    record_limit = opts["recordLimit"]
    if record_limit:
        args += ["--live-record-limit", record_limit]

    print("╔══════════════════════════════════════════════════════╗")
    print("║  📼 VOD 模式 — 存档下载                               ║")
    print("╚══════════════════════════════════════════════════════╝")
    print(f"  🔗 {opts['urlMpd']}")
    print(f"  🎬 标题:     {opts.get('title') or '(none)'}")
    print(f"  📄 文件名:   {save_name}.mp4")
    print(f"  📁 输出目录: {Path(opts['outputDir']).resolve()}")
    print(f"  📝 日志:     {opts.get('logFilePath') or '(none)'}")
    print("  🏷️  容器:     MP4 (moov atom finalized on completion)")
    if opts.get("recordLimit"):
        print(f"  ⏱️  录制上限: {opts['recordLimit']}")
    print()

    run_downloader(opts["downloaderPath"], args)

    print("\n✅ VOD 下载完成")


def download_live(opts: DownloadOpts) -> None:
    """
    LIVE (直播) 下载。
    输出: <标题>_<时间戳>.ts (持续增长的 TS 文件, PotPlayer 可直接打开)

    --live-real-time-merge  实时混流
    --live-pipe-mux         解密后的分片经管道交给 ffmpeg
    --live-keep-segments    保留原始分片 (崩溃后可恢复)
    不使用 --mux-after-done, 保持 TS 容器 (原生支持渐进式读取)

    录制结束后转 MP4: ffmpeg -i <name>.ts -c copy <name>.mp4
    """
    save_name = default_save_name(opts)
    args = build_common_args(opts, save_name)
    args += ["--live-real-time-merge", "--live-pipe-mux", "--live-keep-segments"]

    record_limit = opts["recordLimit"]
    if record_limit:
        args += ["--live-record-limit", record_limit]
    wait_time = opts["waitTime"]
    if wait_time is not None:
        args += ["--live-wait-time", str(wait_time)]

    print("╔══════════════════════════════════════════════════════╗")
    print("║  🔴 LIVE 模式 — 实时录制                              ║")
    print("╚══════════════════════════════════════════════════════╝")
    print(f"  🔗 {opts['urlMpd']}")
    print(f"  🎬 标题:     {opts.get('title') or '(none)'}")
    print(f"  📄 文件名:   {save_name}.ts")
    print(f"  📁 输出目录: {Path(opts['outputDir']).resolve()}")
    print(f"  📝 日志:     {opts.get('logFilePath') or '(none)'}")
    print("  🏷️  容器:     TS (growing — PotPlayer 可直接打开)")
    if opts.get("recordLimit"):
        print(f"  ⏱️  录制上限: {opts['recordLimit']}")
    if opts.get("waitTime"):
        print(f"  🔄 刷新间隔:  {opts['waitTime']}s")
    print("  💡 录制结束后转 MP4: ffmpeg -i <name>.ts -c copy <name>.mp4")
    print()

    run_downloader(opts["downloaderPath"], args)

    print("\n✅ LIVE 录制结束")


# ── 主入口 (对应 cli.ts::main) ────────────────────────────────
def main() -> None:
    global VERBOSE

    args = parse_args()
    keys_only = args.get("keys-only") == "true"
    VERBOSE = args.get("verbose") == "true" or os.getenv("DEBUG") == "true"

    # 参数解析: CLI 参数优先, 其次 .env
    url_mpd = args.get("urlMpd") or os.getenv("URL_MPD")
    cookie_str = args.get("cookieStr") or os.getenv("COOKIE_MPD")
    auth_url = args.get("authUrl") or os.getenv("AUTH_URL")
    player_url = args.get("playerUrl") or os.getenv("PLAYER_URL")
    wvd_path = args.get("wvdPath") or os.getenv("WVD_PATH")

    # 校验必填参数: url_mpd / auth_url 在有 PLAYER_URL 时可省略 (由播放页推导)
    missing: list[str] = []
    if not cookie_str:
        missing.append("COOKIE_MPD (--cookieStr)")
    if not wvd_path:
        missing.append("WVD_PATH (--wvdPath)")
    if not player_url and not url_mpd:
        missing.append("PLAYER_URL (--playerUrl) 或 URL_MPD (--urlMpd)")
    if not player_url and not auth_url:
        missing.append("PLAYER_URL (--playerUrl) 或 AUTH_URL (--authUrl)")
    if missing:
        print("❌ Missing required params:")
        for item in missing:
            print(f"   - {item}")
        if not env_found:
            print("\n未找到 .env 文件：请先将 example.env 复制为 .env 并填写参数")
        print("\nSet them in .env or pass via CLI:")
        print("  uv run main.py --playerUrl=... --cookieStr=... --wvdPath=./bin/xxx.wvd")
        sys.exit(1)

    # 走到这里必填项都已有值, 用 assert 收窄类型供类型检查器使用
    assert cookie_str and wvd_path

    # 模式检测 (--mode 可覆盖自动检测)
    mode = args.get("mode") or (detect_mode(url_mpd) if url_mpd else "live")
    log.info(f"Mode: {mode.upper()}")
    log.info(f"MPD:  {url_mpd or '(derived from PLAYER_URL)'}")

    # 下载相关参数 (默认应用目录下的 ./bin, 与原项目结构一致)
    downloader_path = (
        args.get("downloader")
        or os.getenv("N_M3U8DL_PATH")
        or str(app_path("./bin/N_m3u8DL-RE.exe"))
    )
    ffmpeg_path = (
        args.get("ffmpeg") or os.getenv("FFMPEG_PATH") or str(app_path("./bin/ffmpeg.exe"))
    )
    shaka_packager_path = (
        args.get("shakaPackager")
        or os.getenv("SHAKA_PACKAGER_PATH")
        or str(app_path("./bin/shaka-packager.exe"))
    )
    output_dir = args.get("outputDir") or os.getenv("OUTPUT_DIR") or "./Downloads"
    temp_dir = args.get("tempDir") or os.getenv("TEMP_DIR") or "./Temp"
    log_dir = os.getenv("LOG_DIR") or "./logs"

    # 模式专属参数
    record_limit = args.get("recordLimit") or os.getenv("RECORD_LIMIT")
    wait_time_val = args.get("waitTime") or os.getenv("LIVE_WAIT_TIME")
    wait_time = int(wait_time_val) if wait_time_val else None

    try:
        # ── Step 1: 提取 keys ───────────────────────────────
        log.info(f"Loading WVD: {wvd_path}")
        log.info("Extracting keys...")

        result = get_eplus_keys(
            cookie_str=cookie_str,
            wvd_path=str(app_path(wvd_path)),
            url_mpd=url_mpd,
            auth_url=auth_url,
            player_url=player_url,
        )

        log.info(f"KID:  {result['kid']}")
        log.info(f"Key:  {result['selectedKey']}")
        log.info(f"Base: {result.get('base') or '(none)'}")
        log.info(f"Title: {result.get('title') or '(none)'}")

        if keys_only:
            print("\n── Key data ───────────────────────────────────────────")
            # 不打印 Cookie (含会话凭证)
            printable = {**result, "cookieStr": "(redacted)"}
            print(json.dumps(printable, indent=2, ensure_ascii=False))
            print("\n── N_m3u8DL-RE command ───────────────────────────────")
            print(f'N_m3u8DL-RE "{result["mpdUrl"]}" --key "{result["selectedKey"]}"')
            return

        if not result.get("selectedKey"):
            raise RuntimeError("No key available for download")

        # ── Step 2: 下载 ────────────────────────────────────
        ensure_dir(output_dir)
        ensure_dir(temp_dir)
        ensure_dir(log_dir)

        title = args.get("title") or os.getenv("TITLE") or result.get("title")

        base_opts: DownloadOpts = {
            "urlMpd": result["mpdUrl"],
            "cookieStr": result["cookieStr"],
            "key": result["selectedKey"],
            "downloaderPath": str(app_path(downloader_path)),
            "ffmpegPath": str(app_path(ffmpeg_path)),
            "shakaPackagerPath": str(app_path(shaka_packager_path)),
            "outputDir": str(app_path(output_dir)),
            "tempDir": str(app_path(temp_dir)),
            "logFilePath": str(app_path(log_dir) / f"n_m3u8dl-re_{int(time.time() * 1000)}.log"),
            "threadCount": 16,
            "recordLimit": record_limit,
            "waitTime": wait_time,
            "title": title,
        }

        if mode == "live":
            download_live(base_opts)
        else:
            download_vod(base_opts)

        log.info("All done!")
    except Exception as e:
        print("\n❌ Failed:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
