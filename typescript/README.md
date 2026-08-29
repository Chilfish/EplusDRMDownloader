# Eplus DRM Downloader — TypeScript 参考实现

> 这是本项目的 **TypeScript 参考实现**（基于 `widevine` npm 库），位于仓库的 `typescript/` 目录内。
> 仓库根目录的 `main.py` 是它的 Python 移植版本（以本 TS 实现为准）。
> 本版**仅以源码方式运行，不发布 npm**。

纯命令行工具：一键完成 Eplus DRM 直播/点播的**密钥提取**与**下载**。

## 功能

- 从 Eplus 流媒体 URL 提取 Widevine 解密密钥
- 自动检测模式：`vod.live.eplus.jp` → VOD（点播/回放），`stream.live.eplus.jp` → LIVE（直播）
- 拿到密钥后直接调用 `N_m3u8DL-RE` 下载成片
- 支持 `--keys-only` 只打印密钥与对应的 `N_m3u8DL-RE` 命令

提取流程：

```
MPD URL + Cookie + Auth URL ──► 1. 获取 Auth Token
                                  ├─ 2. 解析 MPD → 提取 KID
                                  ├─ 3. 构建 PSSH
                                  ├─ 4. Widevine CDM 换密钥
                                  └─ 5. 输出 keys ──► N_m3u8DL-RE 下载
```

## 环境要求

- [Bun](https://bun.sh)
- 外部二进制**由用户自行提供**；默认从仓库根目录的 `bin/` 读取（与 Python 版本共享，路径相对项目根解析，可用 `.env` / CLI 参数覆盖）：
  - `N_m3u8DL-RE.exe` — 下载器
  - `ffmpeg.exe` — 转封装/合并
  - `shaka-packager.exe` — 实时 CENC 解密引擎
  - `*.wvd` — Widevine Device 文件（如 `google_aosp_on_ia_emulator_14.0.0_9389cec2_4464_l3.wvd`）

## 安装

在 `typescript/` 目录内安装依赖：

```bash
cd typescript
bun install
```

## 配置

本实现**直接读取项目根目录的 `.env`**（与 Python 版本共用同一份配置），无需在 `typescript/` 内单独准备 `.env`。
所有相对路径均以**项目根目录**为基准解析，因此无论从哪个目录运行都能正确找到 `bin/`、输出目录等。

根目录 `.env`（`example.env` 为模板）：

```bash
URL_MPD=https://stream.live.eplus.jp/out/v1/xxx/index.mpd
COOKIE_MPD="CloudFront-Policy=xxx;xxx=xxx;"
AUTH_URL=https://live.eplus.jp/api/stream/xxx/get_auth_token_drm?channel_id=estp-production-xxx

WVD_PATH=./bin/google_aosp_on_ia_emulator_14.0.0_9389cec2_4464_l3.wvd
FFMPEG_PATH=./bin/ffmpeg.exe
SHAKA_PACKAGER_PATH=./bin/shaka-packager.exe
N_M3U8DL_PATH=./bin/N_m3u8DL-RE.exe
OUTPUT_DIR=./Downloads
TEMP_DIR=./Temp
LOG_DIR=./logs

# RECORD_LIMIT=02:00:00     # 录制时长上限 HH:mm:ss（LIVE/VOD）
# LIVE_WAIT_TIME=30          # 直播分片刷新间隔（秒）
```

## 使用

在**项目根目录**运行（相对路径自动锚定到根目录的 `bin/` 与 `.env`）：

```bash
# 完整流程：提取密钥 → 下载
bun ./typescript/main.ts

# 仅提取密钥，不下载
bun ./typescript/main.ts --keys-only

# 强制直播模式（默认根据 MPD URL 自动判断）
bun ./typescript/main.ts --mode live

# 全部参数通过命令行传入（覆盖 .env）
bun ./typescript/main.ts --urlMpd=... --cookieStr=... --authUrl=... --wvdPath=./bin/xxx.wvd
```

### 参数说明

| CLI 参数 | 对应 `.env` | 说明 |
| --- | --- | --- |
| `--urlMpd` | `URL_MPD` | MPD 播放地址（必填） |
| `--cookieStr` | `COOKIE_MPD` | Eplus 请求 Cookie（必填） |
| `--authUrl` | `AUTH_URL` | 获取 Auth Token 的接口地址（必填） |
| `--wvdPath` | `WVD_PATH` | Widevine Device 文件路径（必填） |
| `--mode` | — | 强制 `vod` / `live`，默认自动检测 |
| `--keys-only` | — | 只提取密钥，跳过下载 |
| `--downloader` | `N_M3U8DL_PATH` | N_m3u8DL-RE 路径 |
| `--ffmpeg` | `FFMPEG_PATH` | ffmpeg 路径 |
| `--shakaPackager` | `SHAKA_PACKAGER_PATH` | shaka-packager 路径 |
| `--outputDir` | `OUTPUT_DIR` | 输出目录，默认 `./Downloads`（相对项目根） |
| `--tempDir` | `TEMP_DIR` | 临时目录，默认 `./Temp`（相对项目根） |
| `--recordLimit` | `RECORD_LIMIT` | 录制时长上限 `HH:mm:ss` |
| `--waitTime` | `LIVE_WAIT_TIME` | 直播分片刷新间隔（秒） |

### `--keys-only` 输出

打印提取结果 JSON（`mpdUrl`、`kid`、`pssh`、`keys`、`selectedKey` 等），并给出一条可直接运行的 `N_m3u8DL-RE` 命令：

```
N_m3u8DL-RE "<mpdUrl>" --key "<kid_hex:key_hex>"
```

## 目录结构

```
main.ts             # CLI 入口（参数解析 + 流程编排 + 路径锚定）
lib/eplus.ts        # 核心：Auth Token → KID → PSSH → Widevine 换密钥（widevine npm 库）
lib/download.ts     # 下载编排（VOD / LIVE）
lib/pssh.ts         # PSSH 构建
lib/parse-cookies.ts # Cookie 解析
bin/ (项目根目录)     # 下载器、ffmpeg、shaka-packager、WVD 等依赖（与 Python 版本共享）
```
