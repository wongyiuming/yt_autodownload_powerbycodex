# 多平台支持与验证记录

实现版本：Host / Chrome 扩展 1.3.0。验证日期：2026-10-23。

## 路径与依赖

| 平台 | 来源识别与集合读取 | 媒体下载 | 登录状态 |
| --- | --- | --- | --- |
| YouTube | 既有 yt-dlp 流程、频道专辑目录 | yt-dlp + FFmpeg | 默认匿名，用户可选当前 Chrome Cookie |
| bilibili | yt-dlp 的 Bilibili 视频、收藏夹、合集、系列与短链接提取器 | yt-dlp + FFmpeg | 默认匿名，用户可选 bilibili Cookie |
| 酷狗 | httpx 读取官网公开页面及完整歌单 API | httpx 流式请求，MP3 原字节保存，其他格式由 FFmpeg 转 MP3 | 无 Cookie，不读取浏览器登录信息 |

`platform_urls.py` 与 `extension/platform_urls.js` 共享 URL fixture，避免弹窗接受的链接在 Host 被拒绝。任务平台由真实 URL 推导，不依赖消息中的平台字段。Cookie 按平台分别存储；YouTube 的 Node/EJS/播放器回退配置不会传给 bilibili。

## bilibili 实际联网验证

使用仓库固定的 `yt-dlp[default]==2026.10.10.233141.dev0`，匿名、只读取元数据：

| 地址 | 结果 |
| --- | --- |
| [收藏夹 ml213003412](https://www.bilibili.com/medialist/detail/ml213003412) | “游戏教学”，7 个 BV 条目 |
| [合集 3662502](https://space.bilibili.com/2142762/lists/3662502?type=season) | “合集·老戴《黑神话悟空》【完结】”，64 个 BV 条目 |
| [视频 BV1iy411i7ab](https://www.bilibili.com/video/BV1iy411i7ab) | 此测试出口的单视频解析失败：`Unable to extract initial state` |

这些结果证明收藏夹和合集读取可以工作；不把它们描述成该环境已经成功完成 B 站媒体下载。公开网页、收藏状态和网络访问行为会变化。自动化回归还使用真实 yt-dlp 提取器的 `suitable()` 检查全部规范化 B 站 URL 与对应提取器匹配。

测试环境的只读 yt-dlp 公网探测使用已有系统信任 CA（`compat_opts={'no-certifi'}`）；没有关闭 TLS 校验，也没有把此环境设置写入产品配置。

## 酷狗实际联网验证

### 完整歌单

地址：[2026年抖音热搜伤感歌曲丨深夜emo必备](https://www.kugou.com/yy/special/single/collection_3_1556150471_784_0.html)。

官网页面只内嵌 30 首；单纯追加网页 `page=2` 参数仍返回同一批预览。实现从 `specialInfo.id` 获得公开歌单 ID，访问官方 `gatewayretry.kugou.com/v2/get_other_list_file` 完整列表端点。使用 `x-router: pubsongscdn.kugou.com` 和公开客户端请求校验格式；该校验不包含用户账号凭据。

新 `KugouClient` 将 `page_size` 设为 25 做实测，完整读取 **4 页、78 首**。另用 `pagesize=2` 验证第 1、2 页返回不同曲目。请求使用随机客户端 `mid`，无 Cookie。代码不接受缺少总数、重复页、错页、提前结束、超出总数或读取中途总数改变的“成功”结果。

完整列表协议参照 [musicdl 上游实现](https://github.com/CharlesPikachu/musicdl/blob/dca2f0967e46a8faf8d279737535471102d74ca3/musicdl/modules/sources/kugou.py) 并通过官方接口核验，代码独立实现；播放信息字段同时核对 [listen1 的酷狗适配器](https://github.com/listen1/listen1_chrome_extension/blob/master/js/provider/kugou.js) 与官方 JSON 响应。

### 真实媒体下载

地址：[Toby Fox — Snowdin Town](https://www.kugou.com/song/#hash=FE2A4AC53981BB3FAF077CCA2461401A)。

新实现无 Cookie 调用公开播放接口，返回 `sharefs.kugou.com` 上的 MP3 直链与 76 秒时长。`KugouClient.download()` 实际下载 **1,220,066 字节**，与 API 声明一致。另外直接调用本项目 `convert_to_mp3()` 验证转换器，成功生成 **1,916,423 字节** MP3；实际队列会保留此类已有 MP3 的原字节，避免二次有损编码。临时媒体在验证后删除，未放入仓库。

原媒体 SHA-256：`13b56227353fa59f512d9d918dda7bc0abb50233c061c323c5a22e27708d2937`。

歌单中的另一个样本返回“需要付费” / Web 接口 `30020`，程序正确报告无可播放直链。一个匿名可播放样本的成功不代表所有会员或登录曲目都能匿名获取地址。此版本没有捕获已登录网页的播放媒体请求。

## 本地与 CI 验证

```bash
python -m unittest discover -s tests -v
node tools/test_platform_urls.js
node tools/test_extension_platforms.js
node tools/test_extension_host_version.js
python native_host/host.py --smoke-local-kugou
python native_host/host.py --smoke-local-mp3
```

- Python 回归涵盖平台路由、登录状态隔离、历史队列兼容、集合分页、试听 / 权限错误、下载完整性、取消、转换和去重。
- 共用 URL fixtures 覆盖 40 个支持案例、49 个拒绝案例。
- 酷狗本地烟雾测试只模拟歌曲元数据，媒体通过真实 localhost HTTP 传输，使用真实 FFmpeg 转换；连续运行两次任务只传输一次。
- Windows Actions 同时运行这些回归、冻结 EXE 的两条实际 MP3 流程、安装器与 Native Messaging 握手。CI 结果以对应提交的工作流为准。

## 队列与失败语义

酷狗一首失败会继续下一首，整张歌单最终标记失败并列出原因，成功曲目保留。点击重试时只重新处理未归档或文件已丢失的歌曲；媒体 HTTP 传输和转换使用临时目录，成功后才原子发布 MP3 并更新下载记录。取消当前酷狗曲目后再次重试会重新下载该曲目，已完成曲目无需重复下载。

酷狗下载同时核对 HTTP 文件长度与播放接口声明的媒体长度。已知试听标记或明显短于完整曲目的播放信息会被拒绝，避免把片段计入完整下载。普通 Host 退出会清理酷狗自有 FFmpeg 子进程；强制结束进程 / 断电不属于这一保证。bilibili 与原 YouTube 流程共享 yt-dlp 后处理行为，视频合并 / 转码期间的取消可能延后到当前后处理结束。

本次变更基于 `main`，没有合并另一条正在审阅的安装器 / 进程树修复 PR #3。
