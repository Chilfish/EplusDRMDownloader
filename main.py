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

# 第三方库依赖
from dotenv import load_dotenv
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

# 初始化日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

MATCH_STREAM = r'https://[vod|stream].live.eplus.jp/out/v1/(?P<base>.*?)/'
MATCH_UUID = r"""cenc:default_KID=\"(?P<mpd_url>.*?)\""""

class EplusDRMDownloader:
    def __init__(self):
        self._load_config()
        self._init_device()
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0'
        })

    def _load_config(self):
        self.url_mpd: str = os.getenv('URL_MPD')
        self.cookie_str: str = os.getenv('COOKIE_MPD')
        self.auth_url: str = os.getenv('AUTH_URL')
        self.wvd_path: str = Path(os.getenv('WVD_PATH', 'device.wvd'))

        self.output_dir = Path(os.getenv('OUTPUT_DIR', 'Downloads'))
        self.temp_dir = Path(os.getenv('TEMP_DIR', 'Temp'))
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.ffmpeg_path = os.getenv('FFMPEG_PATH', 'ffmpeg.exe')
        self.mp4decrypt_path = os.getenv('MP4DECRYPT_PATH', 'mp4decrypt.exe')
        self.downloader_path = os.getenv('N_M3U8DL_PATH', 'N_m3u8DL-RE.exe')

        if not all([self.url_mpd, self.cookie_str, self.auth_url]):
            logger.error("缺少必要的环境变量配置")
            sys.exit(1)

        self.cookies_dict = self._parse_cookies(self.cookie_str)

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
            # 原始逻辑是直接取 json['auth_token']，这里保持一致
            return r.json()['auth_token']
        except Exception as e:
            logger.error(f"获取 Auth Token 失败: {e}")
            sys.exit(1)

    def find_base(self, text: str) -> Optional[str]:
        """还原原本的 Base 查找逻辑"""
        result = re.search(MATCH_STREAM, text)
        if result:
            base_val = result.group("base")
            logger.info(f"获得 Base 值: {base_val}")
            return base_val
        logger.warning("未能通过正则匹配到 Base 值 (但这不一定影响下载)")
        return None

    def createpsshfromkid(self, kid: str) -> str:
        """
        字节级 PSSH 构建逻辑
        pywidevine 自动生成的 PSSH 某些情况下会导致 400 错误，
        这里强制使用 Widevine SystemID 和你原本的 padding 逻辑。
        """
        kid = kid.replace('-', '')
        if len(kid) != 32:
            raise AssertionError('Wrong KID length')

        array_of_bytes = bytearray(b'\x00\x00\x008pssh\x00\x00\x00\x00')
        array_of_bytes.extend(bytes.fromhex('edef8ba979d64acea3c827dcd51d21ed')) # Google System ID
        array_of_bytes.extend(b'\x00\x00\x00\x18\x12\x10')
        array_of_bytes.extend(bytes.fromhex(kid))
        array_of_bytes.extend(b'H\xe3\xdc\x95\x9b\x06') # 原始代码中的 Magic Padding

        return base64.b64encode(bytes.fromhex(array_of_bytes.hex())).decode('utf-8')

    def get_keys(self, pssh_str: str, auth_token: str) -> Optional[str]:
        license_url = "https://lic.drmtoday.com/license-proxy-widevine/cenc/?specConform=true"
        try:
            # 使用 pywidevine 加载手动构建的 PSSH
            pssh_obj = PSSH(pssh_str)
            cdm = Cdm.from_device(self.device)
            session_id = cdm.open()
            challenge = cdm.get_license_challenge(session_id, pssh_obj)

            headers = {
                'accept': '"*/*"',
                'Connection': 'keep-alive',
                'X-Dt-Auth-Token': auth_token,
            }

            logger.info("发送 License 请求...")
            res = requests.post(license_url, headers=headers, data=challenge)

            if res.status_code != 200:
                logger.error(f"License 请求失败 [{res.status_code}]: {res.text}")
                cdm.close(session_id)
                return None

            cdm.parse_license(session_id, res.content)
            keys = cdm.get_keys(session_id)
            cdm.close(session_id)

            key_fin = None
            if len(keys) == 1:
                key_fin = f"{keys[0].kid.hex}:{keys[0].key.hex()}"
            elif len(keys) > 1:
                target_key = keys[1]
                key_fin = f"{target_key.kid.hex}:{target_key.key.hex()}"
                logger.info("检测到多个 Key，已按逻辑选取第二个")

            if key_fin:
                logger.info(f"获得 Key 值: {key_fin}")

            return key_fin

        except Exception as e:
            logger.error(f"CDM/网络异常: {e}")
            return None

    def execute_logic(self):
        """执行主逻辑"""
        try:
            logger.info(f"正在请求 MPD: {self.url_mpd}")
            res = self.session.get(self.url_mpd, cookies=self.cookies_dict)

            if res.status_code != 200:
                logger.error(f"未能访问 MPD 地址，状态码: {res.status_code}")
                return

            self.find_base(res.url)

            m = re.search(MATCH_UUID, res.text)
            if not m:
                logger.error("未在 MPD 中找到 UUID (cenc:default_KID)")
                return

            uuid = m.group("mpd_url")
            logger.info(f"获得 UUID: {uuid}")

            pssh = self.createpsshfromkid(uuid)
            logger.info(f"获得 PSSH 值: {pssh}")

            auth_token = self.get_auth_token()
            mpd_key = self.get_keys(pssh, auth_token)

            if mpd_key:
                self.run_download(mpd_key)
            else:
                logger.error("没有返回正确的 key！检查 auth token 是否有问题")

        except Exception as e:
            logger.exception("运行过程中发生未捕获异常")

    def run_download(self, key: str):
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        save_name = f"eplus_drm_{timestamp}"

        logger.info('=======调用N_m3u8DL-RE下载回放 (实时混流 MP4)=======')

        cmd = [
            self.downloader_path,
            self.url_mpd,
            "--save-name", save_name,
            "--save-dir", str(self.output_dir),
            "--tmp-dir", str(self.temp_dir),
            "--download-retry-count", "5",
            "--auto-select",
            "--thread-count", "16",
            "--live-pipe-mux",
            "--mp4-real-time-decryption",
            "--mux-after-done", "format=mp4",
            "--decryption-binary-path", self.mp4decrypt_path,

            "--check-segments-count",
            "--ffmpeg-binary-path", self.ffmpeg_path,
            "-H", f"Cookie: {self.cookie_str}",
            "-mt",
            "--del-after-done",
            "--key", key
        ]

        try:
            subprocess.run(cmd, check=True)
            logger.info("下载完成")
        except subprocess.CalledProcessError:
            logger.error("N_m3u8DL-RE 异常退出")

if __name__ == "__main__":
    app = EplusDRMDownloader()
    app.execute_logic()
