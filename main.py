import os
import re
import sys
import base64
import requests
import subprocess
import logging
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List

# 依赖库: pip install python-dotenv requests pywidevine pyyaml
from dotenv import load_dotenv
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

# 初始化日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

class EplusDRMDownloader:
    def __init__(self):
        self._load_config()
        self._init_device()
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })

    def _load_config(self):
        """配置加载与严格的环境检查"""
        self.url_mpd: str = os.getenv('URL_MPD')
        self.cookie_str: str = os.getenv('COOKIE_MPD')
        self.auth_url: str = os.getenv('AUTH_URL')

        # 路径处理：强制转换为绝对路径，解决子进程调用找不到文件的问题
        self.wvd_path = Path(os.getenv('WVD_PATH', 'device.wvd')).resolve()
        self.output_dir = Path(os.getenv('OUTPUT_DIR', 'Downloads')).resolve()
        self.temp_dir = Path(os.getenv('TEMP_DIR', 'Temp')).resolve()

        # 创建目录
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # 二进制工具路径 (优先使用环境变量，否则在 PATH 中查找，最后回退到当前目录)
        self.ffmpeg_path = self._resolve_binary(os.getenv('FFMPEG_PATH', 'ffmpeg'))
        self.mp4decrypt_path = self._resolve_binary(os.getenv('MP4DECRYPT_PATH', 'mp4decrypt'))
        self.downloader_path = self._resolve_binary(os.getenv('N_M3U8DL_PATH', 'N_m3u8DL-RE'))

        # 校验核心工具
        if not self.downloader_path or not self.downloader_path.exists():
            logger.error("致命错误: 找不到 N_m3u8DL-RE 下载器。请在 .env 中配置正确路径或将其放入 PATH。")
            sys.exit(1)

        if not self.ffmpeg_path or not self.ffmpeg_path.exists():
            logger.error("致命错误: 找不到 ffmpeg。混流无法进行。")
            sys.exit(1)

        # 实时解密强依赖 mp4decrypt
        if not self.mp4decrypt_path or not self.mp4decrypt_path.exists():
            logger.warning("警告: 找不到 mp4decrypt/shaka-packager，实时解密功能将不可用！")

        if not all([self.url_mpd, self.cookie_str, self.auth_url]):
            logger.error("缺少必要的环境变量配置 (URL_MPD, COOKIE_MPD, AUTH_URL)")
            sys.exit(1)

        self.cookies_dict = self._parse_cookies(self.cookie_str)

    def _resolve_binary(self, name_or_path: str) -> Optional[Path]:
        """解析二进制文件的绝对路径"""
        # 1. 检查是否为直接路径
        p = Path(name_or_path)
        if p.exists() and p.is_file():
            return p.resolve()

        # 2. 在系统 PATH 中查找
        which_path = shutil.which(name_or_path)
        if which_path:
            return Path(which_path).resolve()

        # 3. 检查当前工作目录下的 .exe (Windows)
        cwd_p = Path.cwd() / f"{name_or_path}.exe"
        if cwd_p.exists():
            return cwd_p

        return None

    def _init_device(self):
        try:
            self.device = Device.load(str(self.wvd_path))
        except Exception as e:
            logger.error(f"加载 WVD 设备失败: {e}")
            sys.exit(1)

    @staticmethod
    def _parse_cookies(cookie_str: str) -> Dict[str, str]:
        cookies = {}
        for item in cookie_str.split(';'):
            item = item.strip()
            if '=' in item:
                k, v = item.split('=', 1)
                cookies[k] = v
        return cookies

    def get_auth_token(self) -> str:
        try:
            r = self.session.get(self.auth_url)
            r.raise_for_status()
            return r.json()['auth_token']
        except Exception as e:
            logger.error(f"获取 Auth Token 失败: {e}")
            sys.exit(1)

    def get_keys(self, pssh_str: str, auth_token: str) -> Optional[str]:
        license_url = "https://lic.drmtoday.com/license-proxy-widevine/cenc/?specConform=true"
        try:
            pssh_obj = PSSH(pssh_str)
            cdm = Cdm.from_device(self.device)
            session_id = cdm.open()
            challenge = cdm.get_license_challenge(session_id, pssh_obj)

            headers = {'X-Dt-Auth-Token': auth_token}
            res = self.session.post(license_url, headers=headers, data=challenge)

            if res.status_code != 200:
                logger.error(f"License 请求失败: {res.text}")
                cdm.close(session_id)
                return None

            cdm.parse_license(session_id, res.content)
            keys = cdm.get_keys(session_id)
            cdm.close(session_id)

            for key in keys:
                if key.type == 'CONTENT':
                    return f"{key.kid.hex}:{key.key.hex()}"
            if keys:
                return f"{keys[0].kid.hex}:{keys[0].key.hex()}"
            return None
        except Exception as e:
            logger.error(f"CDM 处理异常: {e}")
            return None

    def _manual_pssh_gen(self, kid_hex: str) -> str:
        # 标准 PSSH 生成逻辑
        system_id = bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")
        kid_bytes = bytes.fromhex(kid_hex.replace('-', ''))
        data = bytearray()
        data.extend(b'\x00\x00\x008pssh\x00\x00\x00\x00')
        data.extend(system_id)
        data.extend(b'\x00\x00\x00\x18\x12\x10')
        data.extend(kid_bytes)
        data.extend(b'H\xe3\xdc\x95\x9b\x06')
        return base64.b64encode(data).decode('utf-8')

    def extract_mpd_info(self) -> tuple[Optional[str], Optional[str]]:
        try:
            res = self.session.get(self.url_mpd, cookies=self.cookies_dict)
            res.raise_for_status()
            mpd_content = res.text

            match_uuid = re.search(r'cenc:default_KID="(?P<kid>[0-9a-fA-F-]+)"', mpd_content)
            if not match_uuid:
                logger.error("MPD 中未找到 cenc:default_KID")
                return None, None

            uuid = match_uuid.group("kid")
            match_pssh = re.search(r'<cenc:pssh>(.*?)</cenc:pssh>', mpd_content)
            pssh = match_pssh.group(1) if match_pssh else self._manual_pssh_gen(uuid)
            return uuid, pssh

        except Exception as e:
            logger.error(f"MPD 解析失败: {e}")
            return None, None

    def run_download(self, key: str):
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        # 确保文件名安全，不含非法字符
        save_name = f"eplus_{timestamp}"

        # 构建命令，使用绝对路径
        cmd: List[str] = [
            str(self.downloader_path),
            self.url_mpd,
            "--save-name", save_name,
            "--save-dir", str(self.output_dir),
            "--tmp-dir", str(self.temp_dir),
            "--download-retry-count", "5",
            "--auto-select",
            "--thread-count", "8",

            "--mp4-real-time-decryption",
            "--decryption-binary-path", str(self.mp4decrypt_path),
            "--mux-after-done", "format=mp4",
            "--ffmpeg-binary-path", str(self.ffmpeg_path),
            "--live-pipe-mux",

            "--check-segments-count",
            "--log-level", "INFO",
            "--key", key,
            "-H", f"Cookie: {self.cookie_str}",
            "-mt"
        ]

        logger.info(f"启动下载器...")
        logger.info(f"输出目录: {self.output_dir}")
        logger.info(f"FFmpeg路径: {self.ffmpeg_path}")

        try:
            # 允许看到 N_m3u8DL-RE 的标准输出
            subprocess.run(cmd, check=True)

            # 检查最终文件是否存在
            final_file = self.output_dir / f"{save_name}.mp4"
            if final_file.exists():
                logger.info(f"✅ 成功! 文件已生成: {final_file}")
                # 成功后再清理临时文件
                logger.info("清理临时文件...")
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            else:
                logger.error("❌ 下载过程似乎完成了，但在输出目录未找到 MP4 文件。")
                logger.error(f"请检查 '{self.temp_dir}' 目录，分片可能仍保留在其中。")
                logger.error("可能原因: ffmpeg 合并失败 (请检查上方日志) 或 磁盘空间不足。")

        except subprocess.CalledProcessError as e:
            logger.error(f"下载器异常退出，退出码: {e.returncode}")

def main():
    app = EplusDRMDownloader()

    uuid, pssh = app.extract_mpd_info()
    if not pssh: return

    auth_token = app.get_auth_token()
    key = app.get_keys(pssh, auth_token)

    if key:
        logger.info(f"获取 Key 成功: {key}")
        app.run_download(key)
    else:
        logger.error("无法获取解密 Key，终止下载")

if __name__ == "__main__":
    main()
