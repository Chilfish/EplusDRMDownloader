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
| `--playerUrl` | 播放页 URL（`/ex/player?ib=...` 或 `?ep=...`）：标题 / MPD / sid / channel_id / 签名 Cookie 全部自动获取 |
| `--title` | 直接指定输出文件名前缀（优先于 `--playerUrl`） |
| `--verbose` | 打印完整 HTTP 响应体，便于排查鉴权问题 |

- **VOD 模式**：下载为 `<标题>_<时间戳>.mp4`（`--live-perform-as-vod` 等全部档案分片，`--mux-after-done` 生成标准 MP4）。
- **LIVE 模式**：实时录制为持续增长的 `<标题>_<时间戳>.ts`，PotPlayer 可直接打开；录制结束后可自行转 MP4：`ffmpeg -i <name>.ts -c copy <name>.mp4`。
- **文件名**：设置了 `PLAYER_URL`（或 `TITLE`）时用直播标题作前缀，标题中的 `/ \ : * ? " < > |` 会被替换为 `_`；未设置时回退为 `eplus_drm_<时间戳>`。启动时的 banner 会显示最终文件名。
- **sid / 签名 Cookie 自动获取**：`AUTH_URL` 里的 `sid` 是一次性的；Eplus 签发的 CloudFront Cookie 有效期约 1 小时。设置 `PLAYER_URL` 后，每次运行都会先打开播放页换取新的 `sid` 与签名 Cookie，无需手动更新 `.env`。超过 1 小时的长时录制需要重新运行（Cookie 到期后分段下载会 403）。

### 备选：TypeScript 参考实现

本仓库还包含一个 **TypeScript 参考实现**（位于 [`typescript/`](./typescript/)，使用 `widevine` npm 库），`main.py` 即由其移植而来，以 TS 实现为准。**本版仅以源码方式运行（不发布 npm）**；外部二进制（N_m3u8DL-RE / ffmpeg / shaka-packager / `*.wvd`）由用户自行提供，默认指向项目根 `bin/`，可用 `.env` 或 CLI 参数覆盖路径。路径自动锚定到项目根，从任意目录均可运行：

```bash
bun ./typescript/main.ts
```

### 🧹 代码质量 (Lint / Typecheck)

项目预置了 [ruff](https://docs.astral.sh/ruff/)（lint + format）与 [pyright](https://github.com/microsoft/pyright)（类型检查），并接入 [pre-commit](https://pre-commit.com/)，均为 `dev` 依赖组、由 `uv` 锁定版本：

```bash
uv run ruff check --fix main.py   # lint 并自动修复
uv run ruff format main.py        # 格式化 (统一引号/排版)
uv run pyright main.py            # 类型检查
uv run pre-commit run --all-files # 手动跑全部钩子
```

`git commit` 时钩子会自动运行以上检查（已通过 `uv run pre-commit install` 安装）。

### 方式二：懒人整合包 (Release)

无需安装 Python 环境，下载即用。

Release 包解压后为**同一目录**，包含：

* `EplusDRMDownloader.exe` — 主程序
* `example.env` — 配置模板（需复制为 `.env` 并填入参数）
* `bin/` — 运行所需的二进制（ffmpeg、shaka-packager、N_m3u8DL-RE、`*.wvd`）
* `README.md` — 使用说明

1.  下载最新版本的 Release 压缩包。
2.  解压至**不含中文或特殊字符**的路径下。
3.  将 `example.env` **复制为 `.env`** 并填入参数（exe 会读取**自身所在目录**下的 `.env`）。
4.  双击运行 `EplusDRMDownloader.exe`。

## 📥 下载与更新

> **注意**：整合包内已包含所有必要的二进制依赖（FFmpeg, Shaka Packager, N_m3u8DL-RE, WVD）。

[📦 **点击下载**](https://github.com/Chilfish/EplusDRMDownloader/releases/latest/download/EplusDRMDownloader.7z)

**⚠️ 运行前必读：**
*   **路径问题**：解压或运行路径中**绝对不能包含非英文字符**（如中文、日文、空格），否则二进制工具调用会失败，导致无法解密。
*   **环境依赖**：Windows 用户如果遇到 `VCRUNTIME140.dll` 报错，请安装 [Microsoft Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170)。

## 🔑 参数获取指南 (DevTools)

只要拿到**播放页地址**和**会话 Cookie** 两个值，其余（MPD、Auth URL、sid、CloudFront 签名 Cookie）全部由脚本自动获取并定时刷新。

请使用 Edge 或 Chrome 浏览器，按 `F12` 打开开发者工具。

### 1. 播放页 URL → `PLAYER_URL`

地址栏里正在观看的地址即可，形如 `https://live.eplus.jp/ex/player?ib=...` 或 `?ep=...`。

> 脚本会从这个页面读取直播标题、`streamSession`、MPD 地址、`channel_id`，并拿到约 1 小时有效期的 CloudFront 签名 Cookie。

### 2. 会话 Cookie → `COOKIE_MPD`

1.  将过滤器修改为 `drm`，播放器开始播放后会出现 `get_auth_token_drm?...` 请求。
2.  点击该请求 -> **Headers** -> **Request Headers** -> 复制 `Cookie` 字段 -> 填入 `.env` 的 `COOKIE_MPD`。

> **只需要 `live.eplus.jp` 的会话 Cookie**（`KOJIN_SHIKIBETSU`、`ci_session` 等）。
> 不必复制 `CloudFront-Policy` / `CloudFront-Signature` / `CloudFront-Key-Pair-Id`，它们会随 `PLAYER_URL` 自动签发并定时刷新。
> 缺少 `ci_session` 时接口会返回 HTTP 200 + `{"error":true}`（脚本会提示这一点）。

> **提示**：会话 Cookie 有效期较长；签名 Cookie 约 1 小时，长时录制会自动分段续录，无需手动更新 `.env`。

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
