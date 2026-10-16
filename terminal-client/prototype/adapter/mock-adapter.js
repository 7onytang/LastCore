import {
  ConnectionPhase,
  ErrorCode,
  ExecutionState,
  NodeAdapterError,
  assertNoSecrets,
  validateConnectProfile,
  validateResourcePatch,
} from "./adapter.js";

const MAX_RECENT_ASSIGNMENTS = 10;
const MAX_ERROR_LOG = 20;
const HEARTBEAT_FAIL_LIMIT = 3;

export const MockScenario = Object.freeze({
  NORMAL: "normal",
  CONTROLLER_DOWN: "controller_down",
  AUTH_REJECTED: "auth_rejected",
  MODEL_UNSUPPORTED: "model_unsupported",
});

export class MockNodeAdapter {
  constructor(options = {}) {
    this.options = {
      connectLatencyMs: 40,
      heartbeatMs: 60,
      retryMs: 40,
      assignmentStartMs: 30,
      stopGraceMs: 80,
      clock: () => Date.now(),
      ...options,
    };
    this.scenario = options.scenario ?? MockScenario.NORMAL;
    this.snapshot = this.#initialSnapshot();
    this.listeners = new Set();
    this.timers = new Set();
    this.journal = [];
    this.assignmentCounter = 0;
    this.connected = false;
  }

  #initialSnapshot() {
    return {
      adapter: "mock",
      connection: { phase: ConnectionPhase.DISCONNECTED, retryCount: 0, lastError: null },
      node: {
        nodeId: null,
        name: "",
        platform: "",
        controllerUrl: "",
        endpoint: "",
        availableMemoryMb: 0,
        computeWeight: 0,
        acceptingTasks: true,
        heartbeat: { lastOkAtMs: null, failCount: 0 },
      },
      execution: null,
      recentAssignments: [],
      errorLog: [],
    };
  }

  #now() {
    return this.options.clock();
  }

  #delay(ms) {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.timers.delete(timer);
        resolve();
      }, ms);
      this.timers.add(timer);
    });
  }

  #publish() {
    assertNoSecrets(this.snapshot);
    const frozen = structuredClone(this.snapshot);
    for (const listener of this.listeners) {
      listener(frozen);
    }
  }

  #logError(source, code, message) {
    this.snapshot.errorLog.unshift({ atMs: this.#now(), source, code, message });
    if (this.snapshot.errorLog.length > MAX_ERROR_LOG) {
      this.snapshot.errorLog.length = MAX_ERROR_LOG;
    }
    this.snapshot.connection.lastError = { source, code, message };
  }

  #clearError(source) {
    if (this.snapshot.connection.lastError?.source === source) {
      this.snapshot.connection.lastError = null;
    }
  }

  subscribe(listener) {
    this.listeners.add(listener);
    listener(structuredClone(this.snapshot));
    return () => this.listeners.delete(listener);
  }

  async connect(profile) {
    if (this.connected) {
      throw new NodeAdapterError(ErrorCode.INVALID_STATE, "已连接，不能重复入网");
    }
    const clean = validateConnectProfile(profile);
    this.snapshot.node.name = clean.nodeName || "mock-node";
    this.snapshot.node.platform = clean.platformName || "prototype";
    this.snapshot.node.controllerUrl = clean.controllerUrl;
    this.snapshot.node.endpoint = clean.endpoint;
    this.snapshot.node.availableMemoryMb = clean.availableMemoryMb;
    this.snapshot.node.computeWeight = clean.computeWeight;
    this.snapshot.connection.phase = ConnectionPhase.CONNECTING;
    this.snapshot.connection.retryCount = 0;
    this.#publish();

    while (true) {
      await this.#delay(this.options.connectLatencyMs);
      if (this.scenario === MockScenario.AUTH_REJECTED) {
        this.snapshot.connection.phase = ConnectionPhase.DISCONNECTED;
        this.#logError(
          "connection",
          ErrorCode.AUTH_REJECTED,
          "凭据被 Controller 拒绝（401）：不自动重新注册，请人工核对注册 token 或清空本地凭据后处理",
        );
        this.#publish();
        throw new NodeAdapterError(
          ErrorCode.AUTH_REJECTED,
          "凭据被 Controller 拒绝（401），已停止自动重试",
        );
      }
      if (this.scenario !== MockScenario.CONTROLLER_DOWN) {
        break;
      }
      this.snapshot.connection.retryCount += 1;
      this.#logError(
        "connection",
        ErrorCode.UNREACHABLE,
        `Controller 不可达，正在重试（第 ${this.snapshot.connection.retryCount} 次）`,
      );
      this.#publish();
      await this.#delay(this.options.retryMs);
    }

    this.connected = true;
    this.snapshot.connection.phase = ConnectionPhase.CONNECTED;
    this.snapshot.connection.retryCount = 0;
    this.#clearError("connection");
    this.snapshot.node.nodeId = `node-mock-${Math.random().toString(16).slice(2, 6)}`;
    this.snapshot.node.heartbeat = { lastOkAtMs: this.#now(), failCount: 0 };
    this.#startHeartbeat();
    this.#publish();
    return structuredClone(this.snapshot);
  }

  #startHeartbeat() {
    const timer = setInterval(() => {
      if (!this.connected) {
        return;
      }
      const heartbeatDown = this.heartbeatDown === true;
      if (heartbeatDown) {
        this.snapshot.node.heartbeat.failCount += 1;
        this.#logError(
          "connection",
          ErrorCode.UNREACHABLE,
          `心跳续约失败（${this.snapshot.node.heartbeat.failCount}/${HEARTBEAT_FAIL_LIMIT}）`,
        );
        if (this.snapshot.node.heartbeat.failCount >= HEARTBEAT_FAIL_LIMIT) {
          this.snapshot.connection.phase = ConnectionPhase.OFFLINE;
        }
      } else {
        this.snapshot.node.heartbeat.failCount = 0;
        this.snapshot.node.heartbeat.lastOkAtMs = this.#now();
        if (this.snapshot.connection.phase === ConnectionPhase.OFFLINE) {
          this.snapshot.connection.phase = ConnectionPhase.CONNECTED;
          this.#clearError("connection");
        }
      }
      this.#publish();
    }, this.options.heartbeatMs);
    this.timers.add(timer);
  }

  async getStatus() {
    return structuredClone(this.snapshot);
  }

  async updateResources(patch) {
    if (!this.connected) {
      throw new NodeAdapterError(ErrorCode.INVALID_STATE, "未连接 Controller，无法更新资源设置");
    }
    const clean = validateResourcePatch(patch);
    await this.#delay(this.options.connectLatencyMs);
    if (this.heartbeatDown === true) {
      throw new NodeAdapterError(ErrorCode.UNREACHABLE, "与 Controller 的心跳中断，设置未生效");
    }
    Object.assign(this.snapshot.node, clean);
    this.#publish();
    return structuredClone(this.snapshot);
  }

  async requestStopExecution() {
    const execution = this.snapshot.execution;
    if (!execution || ![ExecutionState.STARTING, ExecutionState.RUNNING].includes(execution.state)) {
      throw new NodeAdapterError(
        ErrorCode.INVALID_STATE,
        "当前没有可停止的执行（以执行状态为准，而非按钮状态）",
      );
    }
    execution.cancelRequested = true;
    execution.state = ExecutionState.STOPPING;
    this.#publish();
    await this.#delay(this.options.stopGraceMs);
    execution.state = ExecutionState.STOPPED;
    execution.endedAtMs = this.#now();
    this.#recordJournal(execution);
    const view = this.#journalView(execution);
    const entry = this.snapshot.recentAssignments.find(
      (item) => item.assignmentId === execution.assignmentId,
    );
    if (entry) {
      Object.assign(entry, view);
    }
    this.snapshot.execution = null;
    this.#publish();
    return { stoppedAssignmentId: execution.assignmentId, stoppedAtMs: execution.endedAtMs };
  }

  async disconnect() {
    this.connected = false;
    for (const timer of this.timers) {
      clearTimeout(timer);
      clearInterval(timer);
    }
    this.timers.clear();
    this.snapshot.connection.phase = ConnectionPhase.DISCONNECTED;
    this.snapshot.node.heartbeat = { lastOkAtMs: this.snapshot.node.heartbeat.lastOkAtMs, failCount: 0 };
    this.#publish();
    return structuredClone(this.snapshot);
  }

  async getRecentAssignments() {
    return structuredClone(this.snapshot.recentAssignments);
  }

  setScenario(scenario) {
    if (!Object.values(MockScenario).includes(scenario)) {
      throw new NodeAdapterError(ErrorCode.INVALID_INPUT, `未知场景：${scenario}`);
    }
    this.scenario = scenario;
  }

  setHeartbeatDown(down) {
    this.heartbeatDown = down === true;
  }

  async mockStartAssignment() {
    if (!this.connected) {
      throw new NodeAdapterError(ErrorCode.INVALID_STATE, "未连接，无法接收分配");
    }
    if (this.snapshot.execution) {
      throw new NodeAdapterError(ErrorCode.INVALID_STATE, "本地已有执行中的任务（单进程单任务）");
    }
    this.assignmentCounter += 1;
    const assignment = {
      assignmentId: `asg-mock-${this.assignmentCounter}`,
      taskId: `task-mock-${this.assignmentCounter}`,
      modelId: "tiny-demo/qwen-tiny",
      layerStart: 0,
      layerEnd: 8,
      status: ExecutionState.STARTING,
      startedAtMs: this.#now(),
      endedAtMs: null,
      error: null,
    };
    this.snapshot.execution = { ...assignment, cancelRequested: false };
    this.#publish();
    await this.#delay(this.options.assignmentStartMs);
    if (this.scenario === MockScenario.MODEL_UNSUPPORTED) {
      this.snapshot.execution.state = ExecutionState.FAILED;
      this.snapshot.execution.error = "模型不支持：当前节点没有该模型的可用后端/权重";
      this.#logError("execution", ErrorCode.MODEL_UNSUPPORTED, this.snapshot.execution.error);
      this.#recordJournal(this.snapshot.execution);
      this.snapshot.recentAssignments.unshift(this.#journalView(this.snapshot.execution));
      this.snapshot.execution = null;
      this.#publish();
      return;
    }
    this.snapshot.execution.state = ExecutionState.RUNNING;
    assignment.status = ExecutionState.RUNNING;
    this.#recordJournal(assignment);
    this.snapshot.recentAssignments.unshift(this.#journalView(assignment));
    if (this.snapshot.recentAssignments.length > MAX_RECENT_ASSIGNMENTS) {
      this.snapshot.recentAssignments.length = MAX_RECENT_ASSIGNMENTS;
    }
    this.#publish();
  }

  async mockCompleteAssignment() {
    const execution = this.snapshot.execution;
    if (!execution || execution.state !== ExecutionState.RUNNING) {
      throw new NodeAdapterError(ErrorCode.INVALID_STATE, "没有运行中的执行可完成");
    }
    execution.state = ExecutionState.COMPLETED;
    execution.endedAtMs = this.#now();
    this.#recordJournal(execution);
    const view = this.#journalView(execution);
    const entry = this.snapshot.recentAssignments.find(
      (item) => item.assignmentId === execution.assignmentId,
    );
    if (entry) {
      Object.assign(entry, view);
    }
    this.snapshot.execution = null;
    this.#publish();
  }

  #recordJournal(execution) {
    this.journal.push({
      assignmentId: execution.assignmentId,
      taskId: execution.taskId,
      state: execution.state,
      endedAtMs: execution.endedAtMs ?? null,
    });
  }

  #journalView(execution) {
    return {
      assignmentId: execution.assignmentId,
      taskId: execution.taskId,
      modelId: execution.modelId,
      layerStart: execution.layerStart,
      layerEnd: execution.layerEnd,
      status: execution.state,
      startedAtMs: execution.startedAtMs,
      endedAtMs: execution.endedAtMs ?? null,
      error: execution.error ?? null,
    };
  }
}
