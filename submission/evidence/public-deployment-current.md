# 当前公开页面部署补充

[公开页面摘要](public-deployment-eee330.json)记录：北京时间 2026-10-14 13:45:04，工作台页面对应固定公开源码 `eee330cd2c572756dd46e1e6bc446df1b5d2bd7f`。本次运行文件差异仅为 `controller-server/app/ui/index.html`，更新原生 Mac 恢复修复预览下载入口；业务显示时区为 `Asia/Shanghai`。

公开入口采样显示：HTTPS 根路径 GET/HEAD 302 到工作台，`/console` GET/HEAD 与 `/healthz` 为 200，HTTP→HTTPS 为 308。HTML SHA256 为 `3abbc4779bc8d4c630cdf2b573177bd722c2b5fc1f291da8d176c8e8fe5dfd9c`，与固定 Git 源一致；JS/CSS 字节摘要见公开页面摘要。本材料维护仅核对公开摘要与固定仓库文件，没有执行部署或新增公开任务、计算、设备验收。

此前北京时间 2026-10-13 18:41:15 的 `bf5b82e` 历史记录仍保留于[DEPLOYMENT-v0.1.1.json](https://github.com/7onytang/LastCore/releases/download/v0.1.1/DEPLOYMENT-v0.1.1.json)。18:04 的 `cc7c7ba` 部署和 18:08 实际截图仍是历史采样；不改原图来源。正式核心标签保持 `bf5b82e`，完整源码 ZIP 固定 `6977c0919c9df3323cb0ec52e2b8820646df8233`，r2 固定材料源为 `ac5d630faf2c17248780ceaffb881dfb75902921`；不重新打包或替换既有归档。

Mac 恢复修复预览固定 `5a83c1d`，只有 8 项纯 Mock、构建和独立静态审计通过，未新增真机验收。历史 `f9139762` 包的 MacBook Pro 线性/GUI 证据不转移到新包；本次 HTML 下载入口部署同样不证明新客户端已经运行。
