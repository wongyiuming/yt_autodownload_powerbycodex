# GitHub 重复实现调研

在本项目改造开始前，检索了公开 GitHub 项目。

## 高度重合：fengjunda888/youtube-download-extension

- 架构：Chrome Manifest V3 + Native Messaging + yt-dlp。
- 已有：单视频、播放列表解析、多选、MP3、多个任务、进度显示。
- Native host 使用 .NET 8。
- 许可证：MIT。

与本项目需求的差异：本项目要把既有 Python/yt-dlp 逻辑继续利用，明确识别公开频道并将频道作为整体加入串行队列，同时让 Windows 安装完成后日常只操作 Chrome，因此没有直接复制该项目代码，而是采用相同类别的浏览器/本机分层设计。

Repository: https://github.com/fengjunda888/youtube-download-extension

## 部分重合：HelpFreedom/Triangle-Downloader

- 纯浏览器 Manifest V3。
- 能处理当前 watch 页面的视频/音频。
- 使用页面媒体流和 ffmpeg.wasm，不依赖 yt-dlp。
- 不适合作为“播放列表/频道批量队列 + yt-dlp 本机下载”的直接替代。

Repository: https://github.com/HelpFreedom/Triangle-Downloader

## 结论

公开项目里已经存在“Chrome + yt-dlp + Native Messaging”的完整实现，因此本项目不再探索不可行的“Chrome 扩展直接启动 yt-dlp”路线。Chrome 扩展本身不能随意启动本机进程；Native Messaging 是更稳定、权限边界更明确的方式。
