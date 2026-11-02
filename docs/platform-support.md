# 多平台支持与验证记录

当前开发源码：Host / Chrome 扩展 1.4.0，尚未发行。个人歌单协议核验日期：2026-11-02。下文 bilibili 与酷狗公开歌单、公开媒体的联网样本保留自 2026-10-23 的 1.3.0 验证，不代表个人账号流程已实测通过。

已发行的 Windows v0.2.0 包包含 Host / 扩展 1.3.0，没有个人歌单入口。新代码应使用对应提交的源码或 Windows Actions 产物验证；本次不创建 Release 或版本标签。

## 路径与依赖

| 平台 | 来源识别与集合读取 | 媒体下载 | 登录状态 |
| --- | --- | --- | --- |
| YouTube | 既有 yt-dlp 流程、频道专辑目录 | yt-dlp + FFmpeg | 默认匿名，用户可选当前 Chrome Cookie |
| bilibili | yt-dlp 的 Bilibili 视频、收藏夹、合集、系列与短链接提取器 | yt-dlp + FFmpeg | 默认匿名，用户可选 bilibili Cookie |
| 酷狗公开来源 | httpx 读取官网公开页面及完整歌单 API | httpx 流式请求，MP3 原字节保存，其他格式由 FFmpeg 转 MP3 | 无 Cookie，不读取浏览器登录信息 |
| 酷狗个人歌单 | 主站 URL 识别为个人音乐库；使用账号接口分页读取自建、收藏歌单及每张歌单歌曲 | 复用公开播放地址和 HTTP 媒体路径；按账号、歌单建目录 | 自动读取当前 Chrome 的酷狗会话；凭据只驻留内存，任务绑定用户 ID |

`platform_urls.py` 与 `extension/platform_urls.js` 共享 URL fixture，避免弹窗接受的链接在 Host 被拒绝。任务平台由真实 URL 推导，不依赖消息中的平台字段。YouTube / bilibili Cookie 按平台分别存储，酷狗个人会话不写入 Cookie 文件或队列状态；YouTube 的 Node/EJS/播放器回退配置不会传给 bilibili。

## 酷狗个人歌单：协议依据与验证范围（1.4.0）

在已登录酷狗的 Chrome 资料中，扩展将 `https://www.kugou.com/` 识别为个人歌单来源。打开扩展会自动识别入口，用户加入队列后才在下载 worker 中读取全部歌单与歌曲。具体公开歌单和单曲 URL 保留原语义；无需网页提供“我的歌单”链接。

### 已核验的协议依据

- 官方网页加载的 [`getBaseInfo.min.js`](https://staticssl.kugou.com/common/js/min/npm/getBaseInfo.min.js) 在网页环境从 `KuGoo` Cookie 读取 `KugooID` 和 `t`，并处理客户端 appid、mid、dfid 等基础参数。实现只从当前用户提供的浏览器会话中获取身份。
- 官方 [`infSign.min.js`](https://staticssl.kugou.com/common/js/min/infSign.min.js) 定义 H5 参数排序、JSON 请求体和 MD5 请求签名格式。代码中的请求校验常量来自该公开脚本，不是用户 token。
- 歌单列表使用 `gateway.kugou.com/v7/get_all_list`；自建、默认收藏和“我喜欢”使用账号局部 `listid` 请求 `gateway.kugou.com/v4/get_list_all_file`，经 `x-router: cloudlist.service.kugou.com` 路由。H5 参数交叉核对 [MineRadio 的酷狗实现](https://github.com/zzstar101/MineRadio-api/blob/main/src/providers/kugou/client.rs)。
- `total_ver: 0` 请求全量歌单，`type: 2` 包含自建和收藏；不能复用其他客户端的缓存版本号。局部 `listid` 与收藏来源的全局 ID 分开处理，收藏条目使用来源 URL 进入已有的完整公开歌单解析。依据见 [kugou_sdk 的歌单协议记录](https://docs.rs/crate/kugou_sdk/0.2.9/source/docs/protocol/playlist.md)。
- 自建、收藏歌单和歌曲均须完成分页，不能使用网页预览代替完整结果。响应缺少总数和结束标记时继续请求到空页，短页也继续读取，并检查重复页与分页上限。
- 本次对上述两个端点发送没有个人账号会话的签名请求，均收到 **HTTP 200、业务 `status: 0` 与 `error_code: 20010`**。因此 HTTP 成功只说明请求到达接口，不应视为读取成功，更不能把认证失败解释成空歌单。

### 验证尚未覆盖的部分

本次没有用户登录态下的完整端到端实测，不能据匿名响应宣称“当前账号全部歌单已成功取回”。有效 Cookie 能否被当前接口接受、实际账号的全部自建/收藏歌单数量是否一致、平台返回的个别异常歌曲字段仍需真实账号验证。离线 HTTP fixtures 用于验证客户端对认证、分页、取消、错误和边界响应的处理，不代替这项实测。

个人歌单元数据的读取权限也不等同于媒体下载权限。当前仍通过公开播放接口取得歌曲直链，不会将账号 token 附加到媒体 CDN 请求；无完整直链、已知试听和访问受限曲目继续作为失败处理。

收藏来源沿用公开解析流程；私密、下架或无法解析的来源会报告该歌单失败。自建歌单里没有有效歌曲 hash 的记录保留为不可用条目，逐曲报告失败并继续；现有公开解析器遇到缺 hash 的记录仍会报告整张公开歌单读取失败。

本地验证通过 97 项 Python 回归、扩展平台/版本集成测试，以及 46 个有效 URL 和 71 个拒绝 URL 的 JS/Python 共用用例。真实 Native Messaging 进程验证了 1.4.0 握手、缺失/无效登录的错误返回和日志脱敏；本地 HTTP + FFmpeg 流程生成 4,005 字节 MP3，重复执行只发生一次媒体传输。这些验证使用模拟账号元数据或本地生成的音频。

### 会话、恢复和结果完整性

个人歌单任务只在状态中保存账号 ID，不保存 Cookie/token。重试自动重新读取当前 Chrome 的酷狗登录状态，并核对与原任务的账号一致；切号后需要新建任务。Host 退出或重启会丢失内存会话，未完成的个人任务恢复为失败并提示重新登录重试，不会静默改为匿名模式或使用另一账号。

全部歌单读取在后台 worker 中运行，队列只回传计数、当前标题和进度，完整歌曲数据不写入 Native Messaging 队列快照。目录为 `audio/kugou/用户 <用户ID>/<歌单标题> [<歌单ID>]/`；接口确认的空音乐库可正常完成，空歌单保留目录。歌曲 hash 的全局归档用于复用已下载媒体，同曲出现在其他歌单时通过硬链接或复制补齐目标目录，不能直接跳过而留下不完整歌单。

一张歌单或一首歌失败会继续其余内容，并将整个任务标记为失败，已完成文件可在重试时复用。分页必须检查总数、提前结束、重复页、数量变化及安全上限；无法确认完整性时不报告全部成功。

## bilibili 实际联网验证（1.3.0，2026-10-23）

使用仓库固定的 `yt-dlp[default]==2026.10.10.233141.dev0`，匿名、只读取元数据：

| 地址 | 结果 |
| --- | --- |
| [收藏夹 ml213003412](https://www.bilibili.com/medialist/detail/ml213003412) | “游戏教学”，7 个 BV 条目 |
| [合集 3662502](https://space.bilibili.com/2142762/lists/3662502?type=season) | “合集·老戴《黑神话悟空》【完结】”，64 个 BV 条目 |
| [视频 BV1iy411i7ab](https://www.bilibili.com/video/BV1iy411i7ab) | 此测试出口的单视频解析失败：`Unable to extract initial state` |

这些结果证明收藏夹和合集读取可以工作；不把它们描述成该环境已经成功完成 B 站媒体下载。公开网页、收藏状态和网络访问行为会变化。自动化回归还使用真实 yt-dlp 提取器的 `suitable()` 检查全部规范化 B 站 URL 与对应提取器匹配。

测试环境的只读 yt-dlp 公网探测使用已有系统信任 CA（`compat_opts={'no-certifi'}`）；没有关闭 TLS 校验，也没有把此环境设置写入产品配置。

## 酷狗公开来源实际联网验证（1.3.0，2026-10-23）

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

- Python 回归涵盖平台路由、登录状态隔离、历史队列兼容、集合分页、试听 / 权限错误、下载完整性、取消、转换和去重；个人歌单增加账号绑定、会话丢失后的恢复、逐歌单错误处理与同曲多目录复用验证。
- 共用 URL fixtures 覆盖支持入口与拒绝案例；用例数量以当前测试输出为准。个人入口需与已有具体歌曲、公开歌单以及伪造域名区分。
- 酷狗本地烟雾测试只模拟歌曲元数据，媒体通过真实 localhost HTTP 传输，使用真实 FFmpeg 转换；连续运行两次任务只传输一次。
- Windows Actions 同时运行这些回归、冻结 EXE 的两条实际 MP3 流程、安装器与 Native Messaging 握手。CI 结果以对应提交的工作流为准。

## 队列与失败语义

酷狗一首失败会继续下一首，整张歌单最终标记失败并列出原因，成功曲目保留。点击重试时只重新处理未归档或文件已丢失的歌曲；媒体 HTTP 传输和转换使用临时目录，成功后才原子发布 MP3 并更新下载记录。取消当前酷狗曲目后再次重试会重新下载该曲目，已完成曲目无需重复下载。

酷狗下载同时核对 HTTP 文件长度与播放接口声明的媒体长度。已知试听标记或明显短于完整曲目的播放信息会被拒绝，避免把片段计入完整下载。普通 Host 退出会清理酷狗自有 FFmpeg 子进程；强制结束进程 / 断电不属于这一保证。bilibili 与原 YouTube 流程共享 yt-dlp 后处理行为，视频合并 / 转码期间的取消可能延后到当前后处理结束。

本次变更基于 `main`，没有合并另一条正在审阅的安装器 / 进程树修复 PR #3。
