# LastCore 实际运行截图

采集：2026-10-13，北京时间；ego-browser 同一任务空间13/p1。PNG直接来自实际页面，不是绘制的终端、真机画面或拼接。

当前Demo源：63a5c9f0c7e2afa995a23211ef25bcb81e0b4f16。用户补充项目面向多类型边缘设备后，仅重新采集受影响的01/03；其他边缘设备明确标“开发中”，不声称新增设备已经支持或通过验收。

| 文件 | 来源与范围 |
|---|---|
| 01-demo-desktop.png | 63a5c9f本地运行桌面首页，1440×1100；设备示意，历史Paris摘要。 |
| 02-demo-history-replay.png | 保留027aefe实际交互回放截图及原始时间，1440×1320；不是现场推理。 |
| 03-demo-mobile-browser.png | 63a5c9f的390×1000响应式浏览器；不是Android真机。 |
| 04-live-swagger-health.png | 保留实际线上Swagger Execute GET /healthz，1440×1000，HTTP200/statusok，无鉴权。 |
| 05-live-health-response.png | 保留实际线上HTTPS健康响应，1024×420；仅证明该时点可达。 |

每张图片的URL、北京时间、SHA256及按实际采集保留的源码归属见capture-manifest.json；02/04/05的字节与原采集完全一致，未改成新源码或新时间。Demo图采自http://127.0.0.1:8766/，当时尚未公网发布；服务图采自https://8.134.161.123。健康响应本身不证明源码版本或Worker在线。Android真机仍延期；本轮没有创建或重跑模型任务。
