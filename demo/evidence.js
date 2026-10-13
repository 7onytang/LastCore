/* Derived only from the published acceptance summary. No private job data. */
window.LASTCORE_EVIDENCE = {
  "mode": "public_historical_summary",
  "timeZone": "Asia/Shanghai",
  "sourceEvidence": "evidence.json",
  "cases": {
    "generation": {
      "platform": "MACOS / WHOLE-MODEL CLI",
      "prompt": "公开验收：固定 SmolLM2 整模型生成法国首都答案。",
      "resultLabel": "公开结果摘要",
      "displayResult": "Paris",
      "result": "公开记录摘要，非逐字原响应。",
      "elapsedSeconds": null,
      "recordedAt": "2026-10-13T14:03:23.159854+08:00",
      "timeLabel": "公开核验时间",
      "sourceLabel": "模型证明源",
      "sourceCommit": "21c5d4b7eeca8f418ab81570e7453395a2161f01",
      "caseId": "generation",
      "evidenceUrl": "evidence.json",
      "steps": [
        {
          "title": "云端分配",
          "state": "分配",
          "display": "分配中",
          "description": "整模型任务交给一个已注册节点。",
          "note": "说明性回放，非逐帧日志；不重跑任务。"
        },
        {
          "title": "本机生成",
          "state": "执行",
          "display": "本机生成",
          "description": "macOS CLI 调用固定模型与原生后端。",
          "note": "说明性回放，非逐帧日志；不重跑任务。"
        },
        {
          "title": "结果回传",
          "state": "回传",
          "display": "结果回传",
          "description": "真实输出摘要：Paris。",
          "note": "说明性回放，非逐帧日志；不重跑任务。"
        },
        {
          "title": "确认停止",
          "state": "succeeded",
          "display": "Paris",
          "description": "任务成功并完成停止确认。",
          "note": "本机生成结果，云端保存结果与停止确认。 历史记录，非实时状态。"
        }
      ]
    },
    "timeout": {
      "platform": "MACOS / NATIVE TIMEOUT",
      "prompt": "公开验收：原生后端执行达到时间预算。",
      "resultLabel": "公开结束记录",
      "displayResult": "已停止",
      "result": "执行超时，原生后端停止并回收。",
      "elapsedSeconds": null,
      "recordedAt": "2026-10-13T14:03:23.159854+08:00",
      "timeLabel": "公开核验时间",
      "sourceLabel": "模型证明源",
      "sourceCommit": "21c5d4b7eeca8f418ab81570e7453395a2161f01",
      "caseId": "timeout",
      "evidenceUrl": "evidence.json",
      "steps": [
        {
          "title": "云端分配",
          "state": "分配",
          "display": "任务分配",
          "description": "任务进入同一条真实执行链路。",
          "note": "说明性回放，非逐帧日志；不重跑任务。"
        },
        {
          "title": "达到期限",
          "state": "期限",
          "display": "达到期限",
          "description": "原生后端达到执行预算。",
          "note": "说明性回放，非逐帧日志；不重跑任务。"
        },
        {
          "title": "停止回收",
          "state": "回收",
          "display": "停止回收",
          "description": "后台进程停止并被回收。",
          "note": "说明性回放，非逐帧日志；不重跑任务。"
        },
        {
          "title": "确认结束",
          "state": "failed",
          "display": "已停止",
          "description": "云端保存失败状态与停止确认。",
          "note": "超时有结束状态，也有原生进程回收和停止确认。 历史记录，非实时状态。"
        }
      ]
    },
    "cancel": {
      "platform": "MACOS / NATIVE CANCELLATION",
      "prompt": "公开验收：运行中的原生后端收到取消。",
      "resultLabel": "公开结束记录",
      "displayResult": "已取消",
      "result": "后端停止并回收，生成结果丢弃。",
      "elapsedSeconds": null,
      "recordedAt": "2026-10-13T14:03:23.159854+08:00",
      "timeLabel": "公开核验时间",
      "sourceLabel": "模型证明源",
      "sourceCommit": "21c5d4b7eeca8f418ab81570e7453395a2161f01",
      "caseId": "cancel",
      "evidenceUrl": "evidence.json",
      "steps": [
        {
          "title": "正在执行",
          "state": "执行",
          "display": "正在执行",
          "description": "原生后端已进入真实运行。",
          "note": "说明性回放，非逐帧日志；不重跑任务。"
        },
        {
          "title": "收到取消",
          "state": "取消",
          "display": "收到取消",
          "description": "取消请求交给守卫处理。",
          "note": "说明性回放，非逐帧日志；不重跑任务。"
        },
        {
          "title": "清理进程",
          "state": "清理",
          "display": "清理进程",
          "description": "停止并回收后端，丢弃生成结果。",
          "note": "说明性回放，非逐帧日志；不重跑任务。"
        },
        {
          "title": "确认取消",
          "state": "cancelled",
          "display": "已取消",
          "description": "云端保存取消状态与停止确认。",
          "note": "取消后停止执行、丢弃结果，再确认清理结束。 历史记录，非实时状态。"
        }
      ]
    }
  }
};
