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
  uv run main.py --urlMpd=... --cookieStr=... --authUrl=...

默认读取 .env 文件 (python-dotenv)。CLI 参数优先于 .env 值。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

# ── 日志: [YYYY-MM-DD HH:MM:SS] 消息 (与 cli.ts 的 log() 一致) ────
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('eplus-drm')

# Windows 管道/重定向下 stdout 可能是 cp1252, 强制 UTF-8 输出,
# 避免打印 emoji/中文时抛 UnicodeEncodeError (与 cli.ts 的 UTF-8 输出一致)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    except (AttributeError, ValueError):  # pragma: no cover
        pass

load_dotenv()

# ── 常量 ──────────────────────────────────────────────────────
LICENSE_URL = 'https://lic.drmtoday.com/license-proxy-widevine/cenc/?specConform=true'

MPD_KID_RE = re.compile(r'cenc:default_KID="(?P<kid>[^"]+)"')
MPD_BASE_RE = re.compile(r'https://(?:vod|stream)\.live\.eplus\.jp/out/v1/(?P<base>[^/]*)/')

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0'
)

# ── CLI 参数解析 (--key=value 与 --flag) ───────────────────────
def parse_args() -> dict[str, str]:
    """简易 argv 解析, 与 cli.ts 保持一致:
    --urlMpd=x → {'urlMpd': 'x'}, --keys-only → {'keys-only': 'true'}
    """
    args: dict[str, str] = {}
    for arg in sys.argv[1:]:
        if arg.startswith('--'):
            eq_idx = arg.find('=')
            if eq_idx > 2:
                args[arg[2:eq_idx]] = arg[eq_idx + 1:]
            else:
                args[arg[2:]] = 'true'
    return args


# ── 基础工具 (对应 lib/parse-cookies.ts) ───────────────────────
def ensure_dir(dir_path: str) -> None:
    """确保目录存在 (不存在则递归创建)。"""
    Path(dir_path).resolve().mkdir(parents=True, exist_ok=True)


def parse_cookies(cookie_str: str) -> dict[str, str]:
    """把 Cookie 字符串解析为 key-value 字典。"""
    cookies: dict[str, str] = {}
    for item in cookie_str.split(';'):
        item = item.strip()
        if '=' in item:
            k, v = item.split('=', 1)
            cookies[k] = v
    return cookies


# ── PSSH 构建 (对应 lib/pssh.ts) ──────────────────────────────
WIDEVINE_SYSTEM_ID = 'edef8ba979d64acea3c827dcd51d21ed'
MAGIC_PADDING = b'\x48\xe3\xdc\x95\x9b\x06'  # 原始代码中的 Magic Padding


def create_pssh_from_kid(kid: str) -> str:
    """
    字节级 PSSH 构建逻辑。
    pywidevine 自动生成的 PSSH 某些情况下会导致 400 错误,
    这里强制使用 Widevine SystemID 和原始 padding 逻辑。

    结构: size(4) + "pssh"(4) + version/flags(4) + SystemID(16)
          + data_size(4) + PlayReady 头(2) + KID(16) + padding(6)
    """
    kid = kid.replace('-', '')
    if len(kid) != 32:
        raise AssertionError(f'Wrong KID length: expected 32 hex chars, got {len(kid)}')

    box = bytearray(b'\x00\x00\x008pssh\x00\x00\x00\x00')
    box.extend(bytes.fromhex(WIDEVINE_SYSTEM_ID))
    box.extend(b'\x00\x00\x00\x18\x12\x10')
    box.extend(bytes.fromhex(kid))
    box.extend(MAGIC_PADDING)

    return base64.b64encode(bytes(box)).decode('utf-8')


# ── Key 提取 (对应 lib/eplus.ts) ───────────────────────────────
def extract_kid(mpd_text: str) -> Optional[str]:
    """从 MPD 文本中提取 cenc:default_KID。"""
    m = MPD_KID_RE.search(mpd_text)
    return m.group('kid') if m else None


def extract_base(url: str) -> Optional[str]:
    """从流地址中提取 base 值 (可能为 None, 不影响下载)。"""
    m = MPD_BASE_RE.search(url)
    return m.group('base') if m else None


def get_eplus_keys(url_mpd: str, cookie_str: str, auth_url: str, wvd_path: str) -> dict[str, Any]:
    """
    完整 key 提取流程 (对应 cli.ts Step 1 + lib/eplus.ts::getEplusKeys):
      1. 从 AUTH_URL 获取 auth_token
      2. 请求 MPD → 提取 KID、base
      3. 由 KID 构建 Widevine PSSH
      4. 加载 WVD 设备, 创建 CDM 会话
      5. 生成 License Challenge → POST 到 License Server
      6. 解析 License 响应 → 提取内容密钥
    返回与 lib/eplus.ts 相同的 JSON 结构。
    """
    headers = {
        'User-Agent': USER_AGENT,
        'Cookie': cookie_str,
    }

    # ── Step 1: 获取 auth_token ─────────────────────────────
    auth_res = requests.get(auth_url)
    if auth_res.status_code != 200:
        raise RuntimeError(f'Auth token request failed: {auth_res.status_code}')
    auth_token = auth_res.json().get('auth_token')
    if not auth_token:
        raise RuntimeError('No auth_token in auth response')

    # ── Step 2: 请求 MPD, 提取 KID / Base ───────────────────
    mpd_res = requests.get(url_mpd, headers=headers)
    if mpd_res.status_code != 200:
        raise RuntimeError(f'MPD fetch failed: {mpd_res.status_code}')
    mpd_text = mpd_res.text
    final_url = mpd_res.url  # 重定向后的最终 URL

    base = extract_base(final_url)
    kid = extract_kid(mpd_text)
    if not kid:
        raise RuntimeError('Could not find cenc:default_KID in MPD')

    # ── Step 3: 由 KID 构建 PSSH ────────────────────────────
    pssh = create_pssh_from_kid(kid)

    # ── Step 4-6: Widevine CDM → License → keys ─────────────
    device = Device.load(wvd_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    try:
        challenge = cdm.get_license_challenge(session_id, PSSH(pssh))

        license_res = requests.post(
            LICENSE_URL,
            headers={
                'accept': '*/*',
                'Connection': 'keep-alive',
                'X-Dt-Auth-Token': auth_token,
            },
            data=challenge,
        )
        if license_res.status_code != 200:
            raise RuntimeError(
                f'License request failed [{license_res.status_code}]: '
                f'{license_res.text[:500]}'
            )

        cdm.parse_license(session_id, license_res.content)
        raw_keys = cdm.get_keys(session_id)
    finally:
        cdm.close(session_id)

    if not raw_keys:
        raise RuntimeError('No valid keys in license response')

    # 与 cli.ts 相同的 key 选择逻辑:
    #   - 单个 key → 使用第一个
    #   - 多个 key → 使用第二个 (index 1)
    key_containers = [
        {'kid': k.kid.hex, 'key': k.key.hex()}
        for k in raw_keys
    ]
    keys = [f"{k['kid']}:{k['key']}" for k in key_containers]
    selected = key_containers[1] if len(key_containers) > 1 else key_containers[0]

    return {
        'mpdUrl': final_url,
        'base': base,
        'kid': kid,
        'pssh': pssh,
        'keys': keys,
        'keyContainers': key_containers,
        'selectedKey': f"{selected['kid']}:{selected['key']}",
    }


# ── 下载 (对应 lib/download.ts) ───────────────────────────────
def detect_mode(url: str) -> str:
    """从 MPD URL 检测下载模式 (未知主机默认 VOD, 更安全)。"""
    if 'stream.live.eplus.jp' in url:
        return 'live'
    if 'vod.live.eplus.jp' in url:
        return 'vod'
    log.warning(f'⚠️  未知 MPD 主机, 默认按 VOD 处理: {url}')
    return 'vod'


def build_common_args(opts: dict[str, Any]) -> list[str]:
    """两种模式共用的 N_m3u8DL-RE 参数。"""
    save_name = f"eplus_drm_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
    args = [
        opts['urlMpd'],
        '--save-name', save_name,
        '--save-dir', str(Path(opts['outputDir']).resolve()),
        '--tmp-dir', str(Path(opts['tempDir']).resolve()),
        '--download-retry-count', '5',
        '--auto-select',
        '--thread-count', str(opts.get('threadCount', 16)),
        '--mp4-real-time-decryption',
        '--decryption-engine', 'SHAKA_PACKAGER',
        '--decryption-binary-path', str(Path(opts['shakaPackagerPath']).resolve()),
        '--check-segments-count',
        '--ffmpeg-binary-path', str(Path(opts['ffmpegPath']).resolve()),
        '-H', f"Cookie: {opts['cookieStr']}",
        '-mt',
        '--del-after-done',
        '--key', opts['key'],
        '--log-level', 'INFO',
    ]
    if opts.get('logFilePath'):
        args += ['--log-file-path', str(Path(opts['logFilePath']).resolve())]
    return args


def run_downloader(downloader_path: str, args: list[str]) -> None:
    """同步运行 N_m3u8DL-RE, 非零退出码视为失败。"""
    proc = subprocess.run([downloader_path, *args], check=False)
    if proc.returncode != 0:
        raise RuntimeError(f'N_m3u8DL-RE exited with code {proc.returncode}')


def download_vod(opts: dict[str, Any]) -> None:
    """
    VOD (回放/存档) 下载。
    输出: eplus_drm_<时间戳>.mp4

    --live-perform-as-vod  确保所有分片收集完毕再 "done"
    --mux-after-done mp4   得到带 moov atom 的标准 MP4
    """
    args = build_common_args(opts)
    args += ['--live-perform-as-vod', '--mux-after-done', 'format=mp4']

    if opts.get('recordLimit'):
        args += ['--live-record-limit', opts['recordLimit']]

    print('╔══════════════════════════════════════════════════════╗')
    print('║  📼 VOD 模式 — 存档下载                               ║')
    print('╚══════════════════════════════════════════════════════╝')
    print(f'  🔗 {opts["urlMpd"]}')
    print(f'  📁 输出目录: {Path(opts["outputDir"]).resolve()}')
    print(f'  📝 日志:     {opts.get("logFilePath") or "(none)"}')
    print('  🏷️  容器:     MP4 (moov atom finalized on completion)')
    if opts.get('recordLimit'):
        print(f'  ⏱️  录制上限: {opts["recordLimit"]}')
    print()

    run_downloader(opts['downloaderPath'], args)

    print('\n✅ VOD 下载完成')


def download_live(opts: dict[str, Any]) -> None:
    """
    LIVE (直播) 下载。
    输出: eplus_drm_<时间戳>.ts (持续增长的 TS 文件, PotPlayer 可直接打开)

    --live-real-time-merge  实时混流
    --live-pipe-mux         解密后的分片经管道交给 ffmpeg
    --live-keep-segments    保留原始分片 (崩溃后可恢复)
    不使用 --mux-after-done, 保持 TS 容器 (原生支持渐进式读取)

    录制结束后转 MP4: ffmpeg -i <name>.ts -c copy <name>.mp4
    """
    args = build_common_args(opts)
    args += ['--live-real-time-merge', '--live-pipe-mux', '--live-keep-segments']

    if opts.get('recordLimit'):
        args += ['--live-record-limit', opts['recordLimit']]
    if opts.get('waitTime') is not None:
        args += ['--live-wait-time', str(opts['waitTime'])]

    print('╔══════════════════════════════════════════════════════╗')
    print('║  🔴 LIVE 模式 — 实时录制                              ║')
    print('╚══════════════════════════════════════════════════════╝')
    print(f'  🔗 {opts["urlMpd"]}')
    print(f'  📁 输出目录: {Path(opts["outputDir"]).resolve()}')
    print(f'  📝 日志:     {opts.get("logFilePath") or "(none)"}')
    print('  🏷️  容器:     TS (growing — PotPlayer 可直接打开)')
    if opts.get('recordLimit'):
        print(f'  ⏱️  录制上限: {opts["recordLimit"]}')
    if opts.get('waitTime'):
        print(f'  🔄 刷新间隔:  {opts["waitTime"]}s')
    print('  💡 录制结束后转 MP4: ffmpeg -i <name>.ts -c copy <name>.mp4')
    print()

    run_downloader(opts['downloaderPath'], args)

    print('\n✅ LIVE 录制结束')


# ── 主入口 (对应 cli.ts::main) ────────────────────────────────
def main() -> None:
    args = parse_args()
    keys_only = args.get('keys-only') == 'true'

    # 参数解析: CLI 参数优先, 其次 .env
    url_mpd = args.get('urlMpd') or os.getenv('URL_MPD')
    cookie_str = args.get('cookieStr') or os.getenv('COOKIE_MPD')
    auth_url = args.get('authUrl') or os.getenv('AUTH_URL')
    wvd_path = args.get('wvdPath') or os.getenv('WVD_PATH')

    # 校验必填参数
    missing: list[str] = []
    if not url_mpd:
        missing.append('URL_MPD (--urlMpd)')
    if not cookie_str:
        missing.append('COOKIE_MPD (--cookieStr)')
    if not auth_url:
        missing.append('AUTH_URL (--authUrl)')
    if not wvd_path:
        missing.append('WVD_PATH (--wvdPath)')

    if missing:
        print('❌ Missing required params:')
        for m in missing:
            print(f'   - {m}')
        print('\nSet them in .env or pass via CLI:')
        print(
            '  uv run main.py --urlMpd=... --cookieStr=... '
            '--authUrl=... --wvdPath=./bin/xxx.wvd'
        )
        sys.exit(1)

    # 模式检测 (--mode 可覆盖自动检测)
    mode = args.get('mode') or detect_mode(url_mpd)
    log.info(f'Mode: {mode.upper()}')
    log.info(f'MPD:  {url_mpd}')

    # 下载相关参数 (默认 ./bin, 与原项目结构一致)
    downloader_path = (
        args.get('downloader')
        or os.getenv('N_M3U8DL_PATH')
        or './bin/N_m3u8DL-RE.exe'
    )
    ffmpeg_path = (
        args.get('ffmpeg')
        or os.getenv('FFMPEG_PATH')
        or './bin/ffmpeg.exe'
    )
    shaka_packager_path = (
        args.get('shakaPackager')
        or os.getenv('SHAKA_PACKAGER_PATH')
        or './bin/shaka-packager.exe'
    )
    output_dir = args.get('outputDir') or os.getenv('OUTPUT_DIR') or './Downloads'
    temp_dir = args.get('tempDir') or os.getenv('TEMP_DIR') or './Temp'
    log_dir = os.getenv('LOG_DIR') or './logs'

    # 模式专属参数
    record_limit = args.get('recordLimit') or os.getenv('RECORD_LIMIT')
    wait_time_val = args.get('waitTime') or os.getenv('LIVE_WAIT_TIME')
    wait_time = int(wait_time_val) if wait_time_val else None

    try:
        # ── Step 1: 提取 keys ───────────────────────────────
        log.info(f'Loading WVD: {wvd_path}')
        log.info('Extracting keys...')

        result = get_eplus_keys(url_mpd, cookie_str, auth_url, wvd_path)

        log.info(f"KID:  {result['kid']}")
        log.info(f"Key:  {result['selectedKey']}")
        log.info(f"Base: {result.get('base') or '(none)'}")

        if keys_only:
            print('\n── Key data ───────────────────────────────────────────')
            print(json.dumps(result, indent=2, ensure_ascii=False))
            print('\n── N_m3u8DL-RE command ───────────────────────────────')
            print(f'N_m3u8DL-RE "{result["mpdUrl"]}" --key "{result["selectedKey"]}"')
            return

        if not result.get('selectedKey'):
            raise RuntimeError('No key available for download')

        # ── Step 2: 下载 ────────────────────────────────────
        ensure_dir(output_dir)
        ensure_dir(temp_dir)
        ensure_dir(log_dir)

        base_opts = {
            'urlMpd': result['mpdUrl'],
            'cookieStr': cookie_str,
            'key': result['selectedKey'],
            'downloaderPath': str(Path(downloader_path).resolve()),
            'ffmpegPath': str(Path(ffmpeg_path).resolve()),
            'shakaPackagerPath': str(Path(shaka_packager_path).resolve()),
            'outputDir': output_dir,
            'tempDir': temp_dir,
            'logFilePath': str(Path(log_dir) / f'n_m3u8dl-re_{int(time.time() * 1000)}.log'),
        }

        if mode == 'live':
            download_live({
                **base_opts,
                'recordLimit': record_limit,
                'waitTime': wait_time,
            })
        else:
            download_vod({
                **base_opts,
                'recordLimit': record_limit,
            })

        log.info('All done!')
    except Exception as e:
        print('\n❌ Failed:', e)
        sys.exit(1)


if __name__ == '__main__':
    main()