# YT AutoDownload Queue

把原来的 Python/yt-dlp 下载脚本改造成 **Chrome 操作入口 + Windows 本机下载组件**。安装完成后，日常不需要再打开 PyCharm、VS Code 或 Codex：只在 Chrome 中打开视频、播放列表或公开频道，然后从扩展加入队列。

## 已实现

- 自动读取当前 Chrome 标签页 URL。
- 自动识别单个 YouTube 视频、视频播放列表、公开频道（`/@handle`、`/channel/UC...`、`/c/...`、`/user/...`）。
- 如果当前 `watch` URL 带 `list=`，按**整个播放列表**处理。
- 下载前选择：**视频 MP4** 或 **仅音频 MP3**。
- 串行下载队列；当前任务运行时仍可继续追加视频、列表或频道。
- 队列状态持久化；Native host 重连后恢复未完成任务。
- 显示当前条目、进度、速度和 ETA。
- 支持取消任务、清理完成项、打开下载目录。
- 自动从当前 Chrome 资料读取 YouTube cookies 并交给本机 host。
- `download_archive` 去重，同一视频不会因为多个来源被反复下载。
- GitHub Actions 自动生成 Windows 发行包并可发布到 Release。

## v1.1：频道“专辑和单曲”正确分类

频道不再把所有文件扁平地堆在一个目录里。对带有 YouTube **“专辑和单曲 / Releases”** 的频道，Native host 会先读取发行结构，再按发行归档：

```text
Downloads/YT-Autodownload/audio/
└─ 达明一派/
   ├─ MULTIVERSE OF POLYGRAM 55TH ANNIVERSARY - 达明一派/
   │  ├─ 01 - ... [video_id].mp3
   │  ├─ 02 - ... [video_id].mp3
   │  └─ ...
   ├─ TM+M DECADE(NCE)/
   │  └─ ...
   ├─ 石頭記/
   │  └─ ...
   └─ _其他上传/
      └─ 不属于任何发行的公开视频...
```

`video/` 使用相同目录结构，只是文件为 MP4。

### 已经完成第一轮下载怎么办

不用重新下载。

1. 升级到 v0.1.1 或更新版本。
2. Chrome 打开需要修复的频道主页。
3. 打开扩展。
4. 点击 **“整理这个频道已下载的文件”**。
5. Host 只读取频道/专辑元数据，然后扫描已有 MP3/MP4 文件名中的 `[YouTube video id]`，按真实发行归属移动到正确目录。

旧版从一开始就把视频 ID 写在文件名中，例如：

```text
歌曲名 [dQw4w9WgXcQ].mp3
```

因此迁移不依赖模糊的标题匹配，不会因为同名歌曲而猜错。

整理同时处理 `audio/` 和 `video/`。如果一个视频 ID 同时属于“单曲”和“专辑”，Windows/NTFS 下优先创建**硬链接**，让两个发行目录都完整而不额外占一份媒体空间；硬链接失败时才复制文件。

所有移动、硬链接、复制、冲突都会记录在：

```text
%LOCALAPPDATA%\YTAutodownload\organization-journal.jsonl
```

遇到目标位置已经存在但内容不同的文件时，程序不会覆盖，原文件会保留并记录 `conflict`。

### 后续频道下载

以后把一个频道加入队列时，流程自动变为：

```text
读取频道 Releases
    ↓
建立 专辑 -> 曲目(video id) 映射
    ↓
先整理历史已下载文件
    ↓
逐个专辑下载到正确目录
    ↓
下载频道其余公开上传到 _其他上传
    ↓
再次校验归档
```

也就是说，修复按钮是给第一轮历史文件用的；**新下载不需要手动整理**。

## 为什么不是“纯 Chrome 扩展”

Chrome 扩展不能直接可靠执行 `yt-dlp`/FFmpeg，也不适合在浏览器进程里完成频道/播放列表的大批量下载和媒体合并。因此项目采用：

```text
Chrome Popup
    ↓
MV3 background service worker
    ↓ Native Messaging
Windows native host
    ↓
yt-dlp + FFmpeg + Node.js
    ↓
Downloads/YT-Autodownload/{video,audio}
```

UI 全部在 Chrome；下载、合并和文件整理由本机组件完成。

## Windows 安装 / 升级

### Release 包（推荐）

1. 打开仓库 **Releases**。
2. 下载最新的 `yt_autodownload_windows.zip` 并解压。
3. 双击 `Install-Windows.bat`。
4. Chrome 打开 `chrome://extensions`。
5. 开启右上角 **开发者模式**。
6. 第一次安装：点击 **加载已解压的扩展程序**，选择 `extension`。
7. 升级时：如果新包解压到了新目录，请在扩展管理页重新加载/重新选择新的 `extension` 目录。

扩展 ID 固定为：

```text
kcmjdacpahcjfomfmecnamfbialfnink
```

安装脚本按这个 ID 注册 Native Messaging host。

### 直接从源码安装

双击：

```text
Install-Windows.bat
```

如果目录中没有预构建的 `yt_autodownload_host.exe`，安装脚本会使用本机 Python 临时构建一个单文件 host。

## 下载目录

```text
%USERPROFILE%\Downloads\YT-Autodownload\video
%USERPROFILE%\Downloads\YT-Autodownload\audio
```

## 队列行为

- 同时只执行 1 个来源任务，避免多个大频道同时抓取导致资源和风控压力叠加。
- 播放列表/频道在 UI 中表现为一个来源任务，内部由 yt-dlp 逐条处理。
- 关闭 popup 不会停止当前任务。
- Chrome/native host 重启后，之前处于 `downloading` 的任务恢复为 `queued`。
- `download_archive` 会跳过已经成功完成的视频。

## 依赖和打包

源码依赖见 `native_host/requirements.txt`：

- `yt-dlp>=2026.3.3`：这个最低版本包含当前 YouTube Music album 解析修复；
- `imageio-ffmpeg`：提供 FFmpeg binary；
- `opencc-python-reimplemented`：保留旧项目的中文文件名处理能力。

Windows Action 会把 Native host 用 PyInstaller **冻结/打包**成单文件 EXE，并附带 Node.js runtime。这里不是 Go/C/Rust 那种必须编译的项目；这样做只是让最终用户无需安装 Python 环境。

## 诊断

```text
%LOCALAPPDATA%\YTAutodownload\host.log
%LOCALAPPDATA%\YTAutodownload\organization-journal.jsonl
%LOCALAPPDATA%\YTAutodownload\catalogs\
```

`catalogs/` 保存最近识别到的频道发行结构，便于核查“哪个视频被归入哪个专辑”。

## 从旧脚本迁移

本仓库主线已经把旧 `auto_download` 脚本中的关键能力迁移为 Chrome + Native Messaging 工作流。旧 CLI、旧临时 HTTP Cookie bridge 和原先会把音频频道根 URL错误转到 `/playlists` 的逻辑不再作为新运行时依赖。

当前仍聚焦 YouTube 单视频 / 播放列表 / 公开频道工作流；原脚本里的 Bilibili 同步逻辑尚未并入 Chrome UI。

## GitHub 现有实现调研

改造前检查过公开实现。最接近的是：

- `fengjunda888/youtube-download-extension`：MV3 + Native Messaging + yt-dlp；
- `HelpFreedom/Triangle-Downloader`：纯浏览器抓取当前 watch 页面，不适合频道/列表批量队列。

详细记录见 `docs/research.md`。本项目没有复制上述项目代码，而是针对这份既有 Python 工程重新实现。

## 使用边界

只下载你有权下载、且当前账号/网络能够合法访问的内容。私有、已删除、地区限制或账号无权限的内容仍会失败；本项目不绕过访问控制。
