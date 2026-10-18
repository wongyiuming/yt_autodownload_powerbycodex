# YT AutoDownload Queue

把原来的 Python/yt-dlp 下载脚本改造成 **Chrome 操作入口 + Windows 本机下载组件**。安装完成后，日常不需要再打开 PyCharm、VS Code 或 Codex：只在 Chrome 中打开视频、播放列表或公开频道，然后从扩展加入队列。

## v0.1.8：自动激活 Native Host 更新

- **安装器自动切换版本**：验证新 Host 后同时更新 Chrome 32 位/64 位注册表视图，检查注册清单路径，并只关闭本软件安装目录中的旧 Host 进程。Chrome 扩展会在连接断开后自动重连，无需手动关闭 Chrome。
- **杜绝“已连接旧版”的假成功**：扩展会将真正连接的 Host 版本与自身清单版本比较，不匹配时显示红色错误并禁止开始下载；点击「重新连接 Host」也不能再把旧版当成成功。
- **真实升级回归测试**：Windows CI 会运行一个模拟旧 Host 的持久连接，将旧注册项注入两个注册表视图，再验证新版安装能关闭旧进程、纠正注册项并通过 Native Messaging 握手。
- 注意：旧版仍在下载时升级，会中断旧 Host 进程，未完成任务由队列持久化机制在新 Host 上恢复。

### 升级安全修复（待发布）

- Host 版本不匹配时仍可取消已有任务；新下载、重试和频道整理继续被禁止。
- 安装器终止安装目录内 Host 的整棵进程树，包括目录外的 FFmpeg / Node 子进程，并确认退出后才允许新版恢复队列。同版本重装也会执行清理。
- 持续重扫并在每次终止前持久化新发现的进程身份；父进程退出后仍保留其身份和退出时间，以发现快照后新建的 FFmpeg / Node 后代。全部生产者退出后，还需通过一次新的完整扫描才算清理成功。
- 使用核验创建时间后的 Windows 进程句柄终止和确认退出，避免 PID 复用造成误杀。安装中断后重跑会恢复清理日志，安装屏障的时间起点不会被重装重置。
- 如果安装中断造成祖先信息丢失，或一个短命中间进程在两次扫描间产生后代后退出，无法证明归属的新孤儿会阻止升级成功。安装器不会凭猜测终止其他进程；请等待相关进程退出后重跑，仍无法完成时重启 Windows 再安装。重启是旧进程必然全部退出的恢复边界。
- 如果升级中断或进程清理失败，新 Host 会等待，避免与旧进程同时写文件。请重新运行最新版 `Install-Windows.bat` 完成清理；不要手工删除 `%LOCALAPPDATA%\YTAutodownload\upgrade-in-progress`。
- Windows CI 在 PowerShell 7 与 Windows PowerShell 5.1 下覆盖真实 FFmpeg / Node 晚创建、父进程提前退出、文件停止写入与独占打开、安装中断恢复和重复清理。PID 复用使用明确标注的确定性身份模型及真实进程的过期创建时间测试，不声称操作系统在测试期间实际复用了特定 PID。另有真实安装器重复安装、新 Host 屏障及协议握手测试。

## v0.1.7：下载运行时与独立 yt-dlp 对齐

- **公开内容默认匿名下载**，不再无条件注入 Chrome 登录 Cookie；只有在扩展勾选「使用 YouTube 登录 Cookie」时，才会附加登录状态。
- **修复 Windows 升级失败**：为每一组 Host/Node 安装独立的哈希版本目录，不覆盖正在运行的旧 EXE，安装后校验 Host、EJS、Node 与扩展版本，任何失败都显式报错。
- 扩展文件固定安装到 `%LOCALAPPDATA%\YTAutodownload\extension`。首次升级需在 `chrome://extensions` 中将旧的解压目录替换成该稳定目录；以后不必随 ZIP 路径迁移。
- v0.1.8 起，安装器会自动终止已安装的旧 Host 并触发 Chrome 重连，扩展会强制进行版本匹配校验。
- Windows CI 增加：真实 EXE 经本地 HTTP 下载 WAV 并用 FFmpeg 生成 MP3、Chrome Native Messaging 帧协议通信、EXE 被锁定时重复安装。YouTube 公网测试由于 GitHub 出口可能被要求验证码，只作为诊断，不作为下载成功承诺。

升级后请在 PowerShell 使用注册文件查找**当前实际运行的 EXE**（不要再直接运行旧的 `bin\yt_autodownload_host.exe`）：

```powershell
$manifest = Get-Content "$env:LOCALAPPDATA\YTAutodownload\com.wongyiuming.yt_autodownload.json" -Raw | ConvertFrom-Json
& $manifest.path --diagnostics
& $manifest.path --probe-url "https://www.youtube.com/watch?v=S0pt6PEApy0"
```

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
7. 升级时：请加载/重新加载安装后固定的 `%LOCALAPPDATA%\YTAutodownload\extension` 目录，扩展内点击「重新连接 Host」，并检查版本号。

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

- `yt-dlp[default]==2026.10.10.233141.dev0`：固定包含 2026-10-10 上游 YouTube reload 修复的 nightly；`default` 自动安装匹配的 `yt-dlp-ejs`；
- `imageio-ffmpeg`：提供 FFmpeg binary；
- `opencc-python-reimplemented`：保留旧项目的中文文件名处理能力。

Windows Action 会把 Native host 用 PyInstaller **冻结/打包**成单文件 EXE，并附带 Node.js runtime。这里不是 Go/C/Rust 那种必须编译的项目；这样做只是让最终用户无需安装 Python 环境。

## 诊断

此版本的 Native Host 是 1.2.4，Windows 发行版为 v0.1.8。运行以下命令无需打开 Chrome、无需 Cookie、不会下载视频：

```powershell
 $manifest = Get-Content "$env:LOCALAPPDATA\YTAutodownload\com.wongyiuming.yt_autodownload.json" -Raw | ConvertFrom-Json
& $manifest.path --diagnostics
# 进一步测试公开 YouTube 视频的提取与 JS 挑战（不会下载）：
& $manifest.path --probe-url "https://www.youtube.com/watch?v=S0pt6PEApy0"
```

输出包括 `host_version`、`yt_dlp_version`、`ejs_available`、`ejs_version`、`node_version` 和 `node_compatible`。安装包构建时会验证 EJS 已冻结打包、Node.js 版本满足至少 v22 的要求。

默认公开下载不附加 Cookie。只有在用户明确勾选登录模式时才携带 Cookie；如果遇到 YouTube 的 `The page needs to be reloaded`，程序会针对这一错误自动尝试匿名访问；登录专属资源仍需要有效的浏览器 Cookie。每次从 Chrome 添加任务时如果没有获取到 Cookie，旧缓存会被清除。稳定重现的 reload 将在匿名重试失败后再尝试一次备用网页客户端，不会反复执行相同请求。重试任务时扩展也会重新获取 Chrome Cookie。每次启动会在 `host.log` 中写入 `HOST_START` 和依赖版本，方便区分旧进程与新版本。


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
