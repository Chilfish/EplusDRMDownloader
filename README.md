# Eplus DRM Downloader

基于 [Simple_Eplus_DRM_DL](https://github.com/AlanWanco/Simple_Eplus_DRM_DL) 的修改版本。

针对 eplus 平台的 DRM 视频回放、直播下载工具，支持实时解密与 MP4 封装。

## 🚀 快速开始

### 方式一：Python 源码运行 (推荐)

本项目使用 [uv](https://github.com/astral-sh/uv) 进行极速依赖管理。

1.  **初始化环境**

    ```bash
    uv sync
    ```
    
2.  **准备二进制工具**

    确保项目根目录存在 `bin` 文件夹，并包含以下文件（可从 Releases 下载）：
    *   `ffmpeg.exe`
    *   `mp4decrypt.exe` (需安装 VC++ 运行库)
    *   `N_m3u8DL-RE.exe`
    
3.  **配置参数**

    复制 `.env.example` 为 `.env`，并填入参数（获取方法见下文）。
    
4.  **运行**

    ```bash
    uv run main.py
    ```

### 方式二：可执行文件 (Release)

直接下载打包好的 `exe`，在同级目录下创建 `.env` 配置文件并填入参数，直接运行即可。

**⚠️ 注意事项：**
*   **路径问题**：解压或运行路径中**绝对不能包含非英文字符**，否则 `mp4decrypt` 会解密失败。
*   **环境依赖**：Windows 用户需安装 Microsoft Visual C++ Redistributable。

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

> **提示**：Cookie 有效期较短（约1小时），若下载失败请重新获取 Cookie 和 Auth URL。
