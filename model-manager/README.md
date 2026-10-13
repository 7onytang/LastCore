# model-manager

`model-manager/` 是 LC-9 的小模块：把**模型 revision、层边界、后端能力与缓存校验**独立出来，供节点在拿到一份「已获批的小模型 revision」后，安全地把分片落到本地并交给引擎加载。

它只描述和处理**数据**：清单是纯 JSON（相对路径、sha256、形状、字节数、层边界、后端兼容范围、权重许可），解析器 fail closed——任何未声明的键（如 `entrypoint` / `command` / `script`）都会让清单被拒绝；产物字节只被哈希与落盘，**从不被解释或执行**，因此远端清单无法借本模块执行自定义代码。

## 目录

```
model-manager/
├── model_manager/
│   ├── manifest.py        # 清单：严格解析、白名单校验、与引擎布局交叉核对
│   ├── layout.py          # 层/模块字节布局（与 inference-engine 参数布局一致）
│   ├── publish.py         # 由本地文件生成清单（只记录哈希与字节）
│   ├── cost.py            # 层 / embedding / head / dtype / 哈希成本清单
│   ├── cache.py           # 内容寻址缓存：原子落盘、哈希校验、磁盘检查、并发锁
│   ├── materialize.py     # 按清单取字节 → 校验 → 原子落盘 → 完成标记 / verify
│   ├── sources.py         # 本地与 HTTP 产物源（fail closed、路径规范化）
│   ├── engine_adapter.py  # 复用 inference-engine：本段校验后加载 shard/stage
│   ├── cli.py             # python -m model_manager 入口
│   └── errors.py          # 全部错误类型
├── tests/                 # 148 项测试（见「验证」）
├── examples/              # tiny 资产与端到端示例（不下载大权重）
├── requirements*.txt
└── pytest.ini
```

## 快速开始

```bash
cd model-manager
pip install -r requirements-dev.txt          # numpy==2.5.4, pytest==9.1.1
export PYTHONPATH="$(pwd):$(pwd)/../inference-engine:$(pwd)/../communication"

# 1) 造一份 tiny 资产 + 清单（纯本地、无网络；`--out` 是输出目录，默认 examples/assets）
python examples/build_tiny_asset.py --out /tmp/lc9-assets

# 2) 端到端：清单 → 取字节 → 校验 → 原子落盘 → 用引擎加载本段并与单机结果逐字节比对
#    该脚本不接参数：它在自动清理的临时目录（tempfile.TemporaryDirectory）里生成资产与缓存，
#    退出即删除，因此不会留下产物或残留；进程内仍可调用 run(workspace) 指定自己的目录。
python examples/tiny_end_to_end.py

# 3) 命令行
python -m model_manager --help
```

`pytest.ini` 把 `..`/`../inference-engine`/`../communication` 放进 `pythonpath`，所以 **pytest 测试**不需要额外设置；`python -m model_manager` 与 `examples/*.py` 是普通脚本，必须自己带上上面那行 `PYTHONPATH`（仓库内未做打包安装），否则会报 `ModuleNotFoundError: No module named 'inference_engine'`。

## 清单（manifest）

顶层字段（未列出的键一律拒绝）：

| 字段 | 说明 |
| --- | --- |
| `manifest_version` / `model_id` / `revision` | 版本与模型标识（revision 参与缓存路径） |
| `architecture` | 架构白名单，当前仅 `lastcore.tiny-decoder`；其他架构**启动前拒绝** |
| `dtype` | 存储 dtype（`float32` / `float16`），并与 `backends[].dtypes` 交叉核对 |
| `model` | 模型形状，直接构造 `inference_engine.config.ModelConfig` |
| `layer_bytes` / `module_bytes` | 各层与 embedding/head 的字节声明 |
| `license` | `name`、`redistributable`、`identifier`、`source_url`、`notes`（**由调用方提供，本模块不替你判定许可证**） |
| `resources` | `parameter_bytes`、`container_bytes`、`runtime_memory_bytes`、`min_free_disk_bytes` |
| `artifacts[]` | 相对路径、`role`（`tokenizer` / `config` / `checkpoint`）、`sha256`、`size_bytes`、可选 dtype 与层区间 |
| `backends[]` | `dtypes`、`max_seq_len`、`runtime_memory_bytes` |

校验顺序：schema → 架构白名单 → dtype → 模型形状 → 许可 → 资源 → 产物 → 后端 → 与引擎实际参数布局交叉核对。路径做规范化与目录包含性检查（拒绝 `..`、绝对路径、Windows 盘符与符号链接逃逸）。

## 缓存保证（`ModelCache` / `materialize`）

| 关注点 | 行为 |
| --- | --- |
| 原子落盘 | 先写同目录 `*.part-<pid>-<uuid>`，边写边算 sha256，校验大小/哈希后 `fsync` + `os.replace`；目录也 `fsync` |
| 完成标记 | 全部产物就绪后先原子写清单副本、再写 `.complete`（含清单规范形式 sha256）；没有标记的 revision 一律视为未完成，`verify` 明确报错 |
| 完整性 | 缺失、截断、超大、内容被篡改（尺寸相同但哈希不同）都被拒绝并清理 / 可按清单修复 |
| 磁盘 | 落盘前按 `min_free_disk_bytes` 与容器字节检查，**任何写入之前**报 `InsufficientDiskError` |
| 并发 | 同一 `(model_id, revision)` 用 `O_CREAT|O_EXCL` 锁串行化，不同 revision 互不阻塞；持锁进程消失按 pid 判活清理陈旧锁；并发拉取同一 revision 只取一次 |
| 路径安全 | 缓存内相对路径规范化 + 包含性校验 + 符号链接逃逸拒绝 |
| 中断恢复 | 进程中断最多留下 `*.part-*`，可重入续做；不会把半成品当成品 |

## 与引擎的适配

`load_shard_for(...)` / `load_stage_for(...)` 在**校验通过之后**复用 `inference-engine` 的 `load_shard` / `load_stage`，只加载本段参数；`model_config(...)` 直接复用同一 `ModelConfig`，`verify_checkpoint_contents(...)` 把清单声明的层区间与引擎实际参数布局对齐。本模块不修改 `inference-engine/` 与 `communication/`。

## 错误类型

`ManifestError`、`UnsupportedArchitectureError`、`BackendCapabilityError`、`PathTraversalError`、`IntegrityError`、`InsufficientDiskError`、`LockTimeoutError`、`SourceError`、`EngineAdapterError`，统一继承 `ModelManagerError`。

## 验证

```bash
cd model-manager
pytest -q            # 148 passed
PYTHONPATH="$(pwd):$(pwd)/../inference-engine:$(pwd)/../communication" \
  python examples/tiny_end_to_end.py                          # exit 0，三组 prompt 均 bit_identical=true
```

覆盖：清单严格性（未知字段/必填/重复路径/可执行字段/路径穿越）、架构与后端能力拒绝、dtype 与 float16 字节折半、层区间必须完整铺满且不重叠、成本清单各维度、哈希流式与吞吐、缺失/截断/超大/篡改/短写拒绝、API 与真实布局对齐、磁盘不足在写入前报错、原子落盘与完成标记、中断续做、跨进程锁与陈旧锁清理、并发同 revision 只取一次、缓存内符号链接逃逸拒绝、本地/HTTP 源（含 HTTP 缺授权 fail closed）、引擎 shard/stage 加载。

## 许可与资源

`license` 段只**记录**调用方给出的权重许可（名称、是否可再分发、标识符、来源、备注）与 `resources` 资源需求（参数/容器/运行时内存/最小可用磁盘），本模块不代为选择许可证，也不下载大权重。

## 未完成 / 边界

- 尚未接入 `node-agent/` 与 Controller 的任务流程（属 LC-8/LC-11 与集成范围，需 LC-5 D1–D6 批准后另行提交）。
- 未下载或校验真实预训练模型（无存储/下载与金额授权）；只覆盖自建 tiny fixture。
- 磁盘不足、并发与锁用受控 probe 与临时目录验证，未在多机/真实慢盘上实测。
- 尚未添加 `.github/workflows/model-manager.yml`（CI 接入不在本项允许范围内）。
