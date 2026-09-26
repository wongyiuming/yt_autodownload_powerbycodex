# YT AutoDownload Queue

把原来的 Python/yt-dlp 下载脚本改造成 **Chrome 操作入口 + Windows 本机下载组件**。安装完成后，日常不需要再打开 PyCharm、VS Code 或 Codex：只在 Chrome 中打开视频、播放列表或公开频道，然后从扩展加入队列。

## 已实现

- 自动读取当前 Chrome 标签页 URL。
- 自动识别：
  - 单个 YouTube 视频；
  - 视频播放列表；
  - 公开频道（`/@handle`、`/channel/UC...`、`/c/...`、`/user/...`）。
- 如果当前 `watch` URL 带 `list=`，按**整个播放列表**处理。
- 下载前选择：
  - **视频 MP4**：最佳视频 + 最佳音频，FFmpeg 合并；
  - **仅音频 MP3**：最佳音轨转 MP3。
- 串行下载队列：当前任务运行时仍可继续打开别的视频/列表/频道并追加。
- 播放列表、频道交给 yt-dlp 全量展开下载。
- 队列状态持久化；Native host 重连后恢复未完成任务。
- 显示当前条目、列表序号、总体进度、速度和 ETA。
- 支持取消任务、清理完成项、打开下载目录。
- 自动从当前 Chrome 资料读取 YouTube cookies 并交给本机 host，减少登录态/年龄限制导致的解析失败。
- `download_archive` 去重：同一视频即使同时存在于多个列表/频道，也不会重复下载。
- GitHub Actions 自动构建 Windows 单文件 Native Messaging host，并把 Node.js runtime 一起打包。

## 为什么不是“纯 Chrome 扩展”

Chrome 扩展不能直接执行 `yt-dlp`/FFmpeg，也不适合在浏览器进程里完成频道/播放列表的大批量下载和媒体合并。因此项目采用：

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

这样用户界面全部在 Chrome，真正下载仍由本机可靠执行。

## Windows 安装

### 方式 A：GitHub Actions 构建包（推荐）

1. 打开仓库的 **Actions → Windows package**。
2. 下载 `yt_autodownload_windows` artifact 并解压。
3. 双击 `Install-Windows.bat`。
4. Chrome 打开 `chrome://extensions`。
5. 打开右上角 **开发者模式**。
6. 点击 **加载已解压的扩展程序**，选择解压目录中的 `extension`。
7. 扩展 ID 固定为 `kcmjdacpahcjfomfmecnamfbialfnink`，安装脚本已经按这个 ID 注册 Native Messaging host。

完成一次安装后，以后只操作 Chrome 扩展即可。

### 方式 B：直接从源码安装

双击：

```text
Install-Windows.bat
```

如果目录中没有预编译 `yt_autodownload_host.exe`，安装脚本会使用本机 Python 创建临时构建环境，安装依赖并用 PyInstaller 生成 host。之后同样在 `chrome://extensions` 加载 `extension` 目录。

## 使用

1. 打开 YouTube 的一个视频、播放列表或公开频道。
2. 点击扩展。
3. 检查自动识别结果。
4. 选择 **视频 MP4** 或 **仅音频 MP3**。
5. 点击 **加入下载队列**。
6. 继续浏览其他页面并继续加入；下载队列不会被覆盖。

默认输出：

```text
%USERPROFILE%\Downloads\YT-Autodownload\video
%USERPROFILE%\Downloads\YT-Autodownload\audio
```

## 队列行为

- 只同时执行 1 个来源任务，避免多个频道/大列表同时拉取导致磁盘、网络和 YouTube 风控压力叠加。
- 一个“播放列表/频道”在队列中表现为一个来源任务；内部由 yt-dlp 逐条展开。
- 关闭 popup 不会停止任务；MV3 background 与 Native Messaging host 保持连接。
- Chrome/native host 重启后，之前处于 `downloading` 的任务会恢复为 `queued` 再执行；`download_archive` 会跳过已经完成的视频。

## 依赖和打包

源码依赖见 `native_host/requirements.txt`：

- yt-dlp
- imageio-ffmpeg（提供本机 FFmpeg binary）
- OpenCC Python implementation（保留旧项目的中文文件名处理能力，当前下载主流程不强制依赖它）

Windows GitHub Action 会额外把 runner 上的 `node.exe` 放进安装包。Native host 会优先使用同目录 Node，再回退到系统 PATH。

## 旧脚本

上传的原始 `auto_download` Python 源码保留在：

```text
legacy/auto_download/
```

新 Chrome 工作流不依赖旧 CLI，但保留它方便后续迁移 Bilibili、强一致同步和旧 manifest 逻辑。

## GitHub 现有实现调研

改造前已经检查过公开实现。最接近的是：

- `fengjunda888/youtube-download-extension`：MV3 + Native Messaging + yt-dlp，功能与本项目高度重合；
- `HelpFreedom/Triangle-Downloader`：纯浏览器抓取当前 watch 页面，不适合频道/列表批量队列。

详细记录见 `docs/research.md`。本项目没有复制上述项目代码，而是针对这份既有 Python 工程重新实现。

## 使用边界

只下载你有权下载、且当前账号/网络能够合法访问的内容。私有、已删除、地区限制或账号无权限的内容仍会失败；本项目不绕过访问控制。
