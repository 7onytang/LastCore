# 当前公开页面部署补充

2026-10-22 的[只读维护核对](maintenance-2026-10-22.json)确认运行版本仍为 `73b3d1a`，服务健康，`/console`、`/workloads` 及健康接口可达，控制台 JS 与固定源字节一致，未认证节点接口返回 401。公开可信证书已续期，当前有效期为北京时间 2026-10-20 00:27:59 至 2026-10-26 16:27:58；服务器的小时监控最近执行成功。此次没有重启、重新部署、派发任务、模型运行或设备验收。这是采样时状态，不保证未来可用性。

[16 字段公开页面摘要](public-deployment-73b3d1.json)记录：北京时间 2026-10-14 18:00:26，工作台与 Controller 对应固定公开源码 `73b3d1abed7dc9709d522fd8afccc0795ca41fe9`（[PR #89](https://github.com/7onytang/LastCore/pull/89) 合并源）。本次不再只是下载 HTML 更新，运行源码差异的公开路径见摘要；业务显示时区为 `Asia/Shanghai`。

公开入口采样显示：HTTPS 根路径 GET/HEAD 302 到工作台，`/console` GET/HEAD 与 `/healthz` 为 200，HTTP→HTTPS 为 308。HTML SHA256 为 `eaa89a26070afab29691706e6990c3bf8fe5adb917808dba6de383e573798a3d`，与固定 Git 源一致；JS/CSS 字节摘要同样与固定源核对。该 JSON 严格沿用公开字段白名单，不包含内部基础设施、认证、环境、数据库或操作员资料。

部署负责人另核对 [/workloads](https://8.134.161.123/workloads) GET/HEAD、同源 CSP 和 [workloads.js](https://github.com/7onytang/LastCore/blob/73b3d1abed7dc9709d522fd8afccc0795ca41fe9/controller-server/app/ui/workloads.js)、[workloads.css](https://github.com/7onytang/LastCore/blob/73b3d1abed7dc9709d522fd8afccc0795ca41fe9/controller-server/app/ui/workloads.css) 资源可达，与固定源码一致。π/日志功能目前只证明代码、CI 和页面/资源可达；没有新增任务、worker 或节点注册、真实算术执行、模型运行或设备验收。旧节点不会自动具备新能力，须由实际兼容的执行器明确声明；历史 Mac 线性、SmolLM2 和 tiny 执行证据均不能转移到这条新路径。材料维护者只读取这份公开摘要和固定仓库源码，没有执行部署或重跑测试。

此前 `67a0200` 的[2026-10-14 14:04:46 页面摘要](public-deployment-67a020.json)与 `eee330c` 的[13:45:04 页面摘要](public-deployment-eee330.json)保留原字节和采样范围。北京时间 2026-10-13 18:41:15 的 `bf5b82e` 历史记录仍保留于[DEPLOYMENT-v0.1.1.json](https://github.com/7onytang/LastCore/releases/download/v0.1.1/DEPLOYMENT-v0.1.1.json)。18:04 的 `cc7c7ba` 部署和 18:08 实际截图仍是历史采样；不改原图来源。

正式核心标签保持 `bf5b82e`，完整源码 ZIP 固定 `6977c0919c9df3323cb0ec52e2b8820646df8233`，r2 固定材料源为 `ac5d630faf2c17248780ceaffb881dfb75902921`；没有重新生成或替换归档。客户端二进制各自保留来源：Mac 恢复修复预览 `5a83c1d`、Windows/Linux `63d409a8` 及历史 `85a1e232`，Android `1760373`，历史实测 Mac 包 `f9139762`。它们不因 Controller 更新成为 `73b3d1` 构建包，也不获得新增实际任务/设备验收。
