export const ConnectionPhase = Object.freeze({
  DISCONNECTED: "disconnected",
  CONNECTING: "connecting",
  CONNECTED: "connected",
  OFFLINE: "offline",
});

export const ExecutionState = Object.freeze({
  STARTING: "starting",
  RUNNING: "running",
  STOPPING: "stopping",
  STOPPED: "stopped",
  COMPLETED: "completed",
  FAILED: "failed",
});

export const ErrorCode = Object.freeze({
  UNREACHABLE: "UNREACHABLE",
  AUTH_REJECTED: "AUTH_REJECTED",
  MODEL_UNSUPPORTED: "MODEL_UNSUPPORTED",
  CONTROLLER_ERROR: "CONTROLLER_ERROR",
  INVALID_STATE: "INVALID_STATE",
  INVALID_INPUT: "INVALID_INPUT",
  LOCAL_API_UNAVAILABLE: "LOCAL_API_UNAVAILABLE",
});

const SECRET_KEYS = new Set([
  "registrationtoken",
  "nodetoken",
  "token",
  "authorization",
  "password",
  "secret",
]);

export class NodeAdapterError extends Error {
  constructor(code, message, details = null) {
    super(message);
    this.name = "NodeAdapterError";
    this.code = code;
    this.details = details;
  }
}

export function assertNoSecrets(value) {
  const walk = (node, path) => {
    if (node === null || typeof node !== "object") {
      return;
    }
    for (const [key, child] of Object.entries(node)) {
      if (SECRET_KEYS.has(key.toLowerCase())) {
        throw new NodeAdapterError(
          ErrorCode.INVALID_INPUT,
          `secret field leaked into UI state at ${path}.${key}`,
        );
      }
      walk(child, `${path}.${key}`);
    }
  };
  walk(value, "$");
  return value;
}

export function validateConnectProfile(profile) {
  const errors = [];
  const controllerUrl = String(profile.controllerUrl ?? "").trim();
  const endpoint = String(profile.endpoint ?? "").trim();
  const registrationToken = String(profile.registrationToken ?? "");
  const memory = Number(profile.availableMemoryMb);
  const weight = Number(profile.computeWeight);
  if (!/^https?:\/\/\S+$/.test(controllerUrl)) {
    errors.push("Controller 地址必须是以 http(s):// 开头的 URL");
  }
  if (!endpoint) {
    errors.push("本机通信地址（endpoint）不能为空");
  }
  if (!registrationToken) {
    errors.push("注册 token 不能为空");
  }
  if (!Number.isFinite(memory) || memory <= 0) {
    errors.push("可用内存必须是正数（MB）");
  }
  if (!Number.isFinite(weight) || weight <= 0) {
    errors.push("算力权重必须是正数");
  }
  if (errors.length > 0) {
    throw new NodeAdapterError(ErrorCode.INVALID_INPUT, errors.join("；"), { errors });
  }
  return {
    controllerUrl,
    endpoint,
    registrationToken,
    availableMemoryMb: memory,
    computeWeight: weight,
    nodeName: String(profile.nodeName ?? "").trim(),
    platformName: String(profile.platformName ?? "").trim(),
  };
}

export function validateResourcePatch(patch) {
  const clean = {};
  if (patch.availableMemoryMb !== undefined) {
    const memory = Number(patch.availableMemoryMb);
    if (!Number.isFinite(memory) || memory <= 0) {
      throw new NodeAdapterError(ErrorCode.INVALID_INPUT, "可用内存必须是正数（MB）");
    }
    clean.availableMemoryMb = memory;
  }
  if (patch.computeWeight !== undefined) {
    const weight = Number(patch.computeWeight);
    if (!Number.isFinite(weight) || weight <= 0) {
      throw new NodeAdapterError(ErrorCode.INVALID_INPUT, "算力权重必须是正数");
    }
    clean.computeWeight = weight;
  }
  if (patch.acceptingTasks !== undefined) {
    clean.acceptingTasks = Boolean(patch.acceptingTasks);
  }
  if (Object.keys(clean).length === 0) {
    throw new NodeAdapterError(ErrorCode.INVALID_INPUT, "资源设置至少包含一项可更新字段");
  }
  return clean;
}
