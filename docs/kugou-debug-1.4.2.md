# 酷狗个人歌单：1.4.2 调试构建

## 本次已确认的问题

1. 1.4.1 的预览 worker 将非认证异常统一改写成“接口请求失败，请检查网络”。因此截图无法区分 HTTP、业务拒绝、分页/结构问题和本机异常；不能从这句话认定用户网络或登录出错。
2. 预览进入 error 后，底部操作提示仍停留在“正在读取”。

1.4.2 修复上述错误报告链路，保留只读预览、同账号确认和已确认歌单快照。Host 和扩展都需要更新至 1.4.2。此次不是已证明解决用户真实账号问题的声明；真实账号测试由用户进行。

## 参考实现与协议边界

- [MakcRe/KuGouMusicApi user_playlist.js](https://github.com/MakcRe/KuGouMusicApi/blob/5d6c9f7b180279c9dd08bc6f7159de52a856d587/module/user_playlist.js)：核对列表路径、路由、分页参数；它使用 Android 协议，不能将 Android appid/签名参数直接与当前网页登录令牌混用。
- [MakcRe/KuGouMusicApi helper.js](https://github.com/MakcRe/KuGouMusicApi/blob/5d6c9f7b180279c9dd08bc6f7159de52a856d587/util/helper.js)：H5 校验串包含排序后的参数及实际 JSON body。新增独立 Node 计算与 Python 请求签名对照测试。结果表明当前实现与该算法一致，不能把签名算法误报为已定位根因。
- [MineRadio-api client.rs](https://github.com/zzstar101/MineRadio-api/blob/2f7196bedf02fd317a14d65cc1784045c685146b/src/providers/kugou/client.rs)：参考网页登录 H5 appid、时间戳、请求体、请求路由和桌面 User-Agent；不复制全量 Cookie 转发。
- [TuneWeave library.rs](https://github.com/MOPELotus/TuneWeave/blob/b6e35c0999965d56ba61b2cfc146218b5763b343/crates/tuneweave-provider-kugou/src/account/library.rs)：原生库的数据模型与完整分页实现。分类计数 list_count/collect_count/album_count 不能简单相加作为当前 info 的总数；保留读到结束标记/空页及一致性校验。原生 v8/曲目 v3 并非本项目网页登录 H5 的无条件替代。

仅参考协议和设计，不引入公共第三方 API 服务，不将用户 Cookie/token 交给开源项目的演示站。保留现有 H5 列表/曲目路径，不增加登录、验证码模拟或权限绕过。

## 诊断格式

失败时显示并可复制：版本、固定错误类别、阶段、固定接口路径（无查询串）、页码、HTTP 状态、业务数字错误码、已知 JSON 字段的类型。程序内部错误只显示白名单异常类型和本项目文件名/行号，不包含异常原文、局部变量或绝对路径。

类别包括 NETWORK_TIMEOUT / NETWORK_PROXY / NETWORK_CONNECT / NETWORK_DNS / NETWORK_TLS / NETWORK_TRANSPORT、HTTP_ERROR、API_REJECTED、SESSION_REJECTED、INVALID_JSON、INVALID_RESPONSE、PAGINATION_*、VERIFICATION_REQUIRED、INTERNAL_ERROR。20010 表示该会话未通过当前接口授权，不能推断网页没有登录。SSA 验证要求只报告，不尝试绕过。

诊断不含 Cookie、token、签名、用户 ID、设备 ID、请求 URL 查询串、响应正文或服务器错误消息。前后端分别实行字段和值白名单。新增 list/items 记录容器兼容与响应账号一致性校验；缺少记录字段仍是错误，不能假装空库。

## 使用与验收

安装本次 Actions 包，重新加载固定目录中的扩展，确认双方为 1.4.2。在已登录的酷狗主站点击“读取歌单 URL”。成功时检查全部名称、数量和 URL；失败时点击“复制脱敏诊断”，反馈这段 JSON 即可，无需截图 Cookie、复制 token 或上传完整日志。读取失败时确认下载保持禁用。真实账号未验收前不要把本构建当作已稳定支持所有个人歌单。

## 自动化验证

新增 17 项 Python 回归（包含独立 Node 签名对照与 popup 状态/白名单检查）。覆盖传输错误、HTTP/业务/认证分离、非 JSON、验证要求、重定向阻断、第二页错误不发布部分库、内部异常脱敏、结构类型摘要、分类计数不作总数、账号不一致以及前端不再停留在“正在读取”。既有全套回归仍由 Windows CI 运行。
