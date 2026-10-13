# LastCore 实际运行截图

采集：2026-10-13，北京时间；ego-browser 同一任务空间13/p1。PNG直接来自真实页面，不是绘制的终端、真机画面或拼接。

| 文件 | 实际来源与范围 |
|---|---|
| 01-demo-desktop.png | 本地运行的固定027aefe Demo桌面首页。设备画面为架构示意；Paris是公开历史摘要。 |
| 02-demo-history-replay.png | 实际点击成功、超时、取消、播放与重置后的Demo回放UI；不是现场推理或逐帧运行日志。 |
| 03-demo-mobile-browser.png | 390px浏览器响应式Demo；不是Android手机真机截图。 |
| 04-live-swagger-health.png | 线上HTTPS Swagger实际Execute GET /healthz，返回HTTP200与statusok；无鉴权输入。 |
| 05-live-health-response.png | 线上HTTPS /healthz实际浏览器响应；仅证明该时点服务可达。 |

Demo三图采自http://127.0.0.1:8766/，截图时尚未公网发布。服务两图采自https://8.134.161.123。详细URL、采集时间、像素尺寸、SHA256及原部署源码归属见capture-manifest.json；健康响应本身不证明源码版本、Worker在线或模型执行。Android真机验收仍延期。本轮没有创建或重跑模型任务。
