# 项目协作说明

- 生图优先用 `sub2api_image`。若当前环境不可用，应说明实际情况，不得声称已使用该工具。
- 每条输出信息仔细审查其来源真伪，不着急产出，不要编造；有不确定的可以指出。
- 项目已采用 Python/FastAPI/SQLite 控制面、Python/NumPy 节点与 tiny 推理、Swift 原生 Mac 客户端、Java/JNI Android 客户端及独立 llama.cpp 整模型路径。沿用各模块现有实现与协议；具体入口见 README 和 [CONTRIBUTING.md](CONTRIBUTING.md)，不要把项目当作尚未选栈的新工程。
- 按 [贡献与协作说明](CONTRIBUTING.md) 隔离分支、提交连贯改动、创建 Draft PR 并记录适用验证和固定源码；功能改动同步维护 README 的安装、运行及验证说明。本文件只补充项目规则，不替代上级个人偏好或当前用户授权。
