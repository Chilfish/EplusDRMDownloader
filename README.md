# Eplus DRM Downloader

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)

基于 [Simple_Eplus_DRM_DL](https://github.com/AlanWanco/Simple_Eplus_DRM_DL) 的重构与增强版本。

针对 eplus 平台的 DRM 视频回放、直播下载工具，实现了 **自动提取 PSSH**、**License 请求伪装** 以及 **实时解密混流 (Real-time Decryption & Muxing)**，输出即为可播放的 ts 文件。

## 🚀 快速开始

### 方式一：Python 源码运行

本项目使用 [uv](https://github.com/astral-sh/uv) 进行极速依赖管理，确保环境纯净。

1.  **初始化环境**

    ```bash
    # 如果未安装 uv: pip install uv
    uv sync
    ```
  
2.  **准备二进制工具**

    确保项目根目录存在 `bin` 文件夹，并包含以下文件（可从 Releases 下载或手动收集）：
    *   `ffmpeg.exe` (处理混流)
    *   `shaka-packager.exe` (Shaka Packager，实时 CENC 解密引擎)
    *   `N_m3u8DL-RE.exe` (核心下载器)
    *   `*.wvd` (Widevine 设备文件，如 `google_aosp_on_ia_emulator_14.0.0_9389cec2_4464_l3.wvd`)

    > `mp4decrypt` 已被 Shaka Packager 取代，不再需要。

3.  **配置参数**

    复制 `.env.example` 为 `.env`，并填入参数（获取方法见下文）。
  
4.  **运行**

    ```bash
    uv run main.py                          # 完整流程: 提取 key → 下载
    uv run main.py --keys-only              # 只提取 key, 跳过下载
    uv run main.py --mode live              # 强制直播模式
    ```

### CLI 用法

模式从 MPD URL **自动检测**（`vod.live.eplus.jp` → VOD，`stream.live.eplus.jp` → LIVE)，也可用 `--mode vod|live` 覆盖。

命令行参数**优先于 `.env`**，可临时覆盖任意配置：

```bash
uv run main.py --urlMpd=... --cookieStr=... --authUrl=... --wvdPath=./bin/xxx.wvd \
  --outputDir=./Downloads --tempDir=./Temp --recordLimit=02:00:00 --waitTime=30
```

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--keys-only` | 只提取 key（输出 JSON 与 N_m3u8DL-RE 命令），跳过下载 |
| `--mode vod\|live` | 强制下载模式，覆盖自动检测 |
| `--urlMpd` / `--cookieStr` / `--authUrl` / `--wvdPath` | 覆盖 `.env` 中的核心参数 |
| `--downloader` / `--ffmpeg` / `--shakaPackager` | 覆盖二进制路径 |
| `--outputDir` / `--tempDir` / `--logDir` | 覆盖输出/临时/日志目录 |
| `--recordLimit` | 录制时长上限 `HH:mm:ss`（VOD 与 LIVE 均适用） |
| `--waitTime` | 直播分片刷新间隔（秒），仅 LIVE 模式 |

- **VOD 模式**：下载为 `eplus_drm_<时间戳>.mp4`（`--live-perform-as-vod` 等全部档案分片，`--mux-after-done` 生成标准 MP4）。
- **LIVE 模式**：实时录制为持续增长的 `eplus_drm_<时间戳>.ts`，PotPlayer 可直接打开；录制结束后可自行转 MP4：`ffmpeg -i <name>.ts -c copy <name>.mp4`。

### 备选：TypeScript 参考实现

本仓库还包含一个 **TypeScript 参考实现**（位于 [`typescript/`](./typescript/)，使用 `widevine` npm 库），`main.py` 即由其移植而来，以 TS 实现为准。两者共用项目根目录的 `bin/` 与 `.env`，路径自动锚定到项目根，从任意目录均可运行：

```bash
bun ./typescript/main.ts
```

### 方式二：懒人整合包 (Release)

无需安装 Python 环境，下载即用。

1.  下载最新版本的 Release 压缩包。
2.  解压至**不含中文或特殊字符**的路径下。
3.  在同级目录下创建 `.env` 配置文件并填入参数。
4.  双击运行 `EplusDRMDownloader.exe`。

## 📥 下载与更新

> **注意**：整合包内已包含所有必要的二进制依赖（FFmpeg, Shaka Packager, N_m3u8DL-RE, WVD）。

[📦 **点击下载**](https://github.com/Chilfish/EplusDRMDownloader/releases/latest/download/EplusDRMDownloader.7z)

**⚠️ 运行前必读：**
*   **路径问题**：解压或运行路径中**绝对不能包含非英文字符**（如中文、日文、空格），否则二进制工具调用会失败，导致无法解密。
*   **环境依赖**：Windows 用户如果遇到 `VCRUNTIME140.dll` 报错，请安装 [Microsoft Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170)。

## 🔑 参数获取指南 (DevTools)

下载回放需要三个核心参数：`MPD地址`、`Cookie`、`Auth验证URL`。

请使用 Edge 或 Chrome 浏览器，按 `F12` 打开开发者工具。

### 1. MPD 地址 & Cookie

1.  进入 **Network (网络)** 面板，勾选 **Preserve log (保留日志)** 和 **Disable cache (禁用缓存)**。
2.  在过滤器中输入 `mpd`。
3.  刷新回放页面，点击列表中出现的 `.mpd` 请求。
    *   **URL**: 右键请求 -> Copy URL -> 填入 `.env` 的 `URL_MPD`。
    *   **Cookie**: 查看 **Headers (标头)** -> **Request Headers (请求标头)** -> 复制 `Cookie` 字段的全部内容 -> 填入 `.env` 的 `COOKIE_MPD`。

### 2. Auth 验证 URL

1.  保持开发者工具打开，将过滤器修改为 `drm`。
2.  点击播放器开始播放视频。
3.  网络面板会出现 `get_auth_token_drm?...` 开头的请求。
4.  右键复制该请求的完整 URL -> 填入 `.env` 的 `AUTH_URL`。

> **提示**：Cookie 和 Auth Token 的有效期较短（通常约 1 小时），若下载失败（403/400 错误），请重新获取并更新 `.env`。

## 🤝 致谢

本项目站在巨人的肩膀上，核心功能离不开以下开源项目的支持：

*   [**Simple_Eplus_DRM_DL**](https://github.com/AlanWanco/Simple_Eplus_DRM_DL): 本项目的原型灵感来源，感谢原作者的探索。
*   [**N_m3u8DL-RE**](https://github.com/nilaoda/N_m3u8DL-RE): @nilaoda 开发的顶级流媒体下载器，支持实时混流。
*   [**Shaka Packager**](https://github.com/shaka-project/shaka-packager): Google 开源的媒体打包/解密工具，用于实时 CENC 解密。
*   [**FFmpeg**](https://ffmpeg.org/): 音视频处理领域的工业标准。
*   [**pywidevine**](https://github.com/devine-dl/pywidevine): 优秀的 Widevine CDM 协议实现库。

## ⚖️ 免责声明

1.  **仅供技术研究**：本工具仅供个人学习 Python 网络编程、DRM 协议分析以及加密流媒体技术研究之用，**严禁用于任何商业用途**。
2.  **版权保护**：请严格遵守相关法律法规及内容提供商的服务条款。使用者下载的内容仅限个人观看，请在 **24小时内删除**，切勿进行公开传播、分发、出租或销售。
3.  **无担保声明**：本软件按“原样”提供，不提供任何形式的明示或暗示担保。开发者不对因使用本工具导致的任何账号封禁、数据丢失或法律纠纷承担责任。
4.  **合法合规**：如果您是版权方并认为本项目侵犯了您的权益，请联系我们进行处理。使用本工具即代表您已阅读并同意上述条款。
