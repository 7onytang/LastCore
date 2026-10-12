import {
  ConnectionPhase,
  ErrorCode,
  NodeAdapterError,
  assertNoSecrets,
  validateConnectProfile,
  validateResourcePatch,
} from "./adapter.js";

export class LocalHttpNodeAdapter {
  constructor(baseUrl, options = {}) {
    this.baseUrl = String(baseUrl ?? "").replace(/\/+$/, "");
    this.options = { timeoutMs: 3000, ...options };
    this.snapshot = this.#initialSnapshot();
    this.listeners = new Set();
  }

  #initialSnapshot() {
    return {
      adapter: "local-http",
      localApi: { baseUrl: this.baseUrl, approved: false },
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

  #localError(method) {
    return new NodeAdapterError(
      ErrorCode.LOCAL_API_UNAVAILABLE,
      `本地 Node API 尚未实现（${method} ${this.baseUrl}）：该端点方案待 LC-3/LC-5 批准后由 node-agent 提供`,
    );
  }

  async #request(method, path, body) {
    assertNoSecrets(body ?? {});
    let response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        method,
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(this.options.timeoutMs),
      });
    } catch (cause) {
      throw new NodeAdapterError(
        ErrorCode.LOCAL_API_UNAVAILABLE,
        `本地 Node API 不可达（${method} ${path}）：${cause?.message ?? cause}`,
      );
    }
    if (!response.ok) {
      let detail = "";
      try {
        detail = JSON.stringify(await response.json());
      } catch {
        detail = await response.text().catch(() => "");
      }
      if (response.status === 401 || response.status === 403) {
        throw new NodeAdapterError(
          ErrorCode.AUTH_REJECTED,
          `本地 API 回环凭据被拒绝（${response.status}）：${detail}`,
        );
      }
      throw new NodeAdapterError(
        ErrorCode.CONTROLLER_ERROR,
        `本地 API 返回 ${response.status}：${detail}`,
      );
    }
    return response.json();
  }

  #publish() {
    assertNoSecrets(this.snapshot);
    const frozen = structuredClone(this.snapshot);
    for (const listener of this.listeners) {
      listener(frozen);
    }
  }

  subscribe(listener) {
    this.listeners.add(listener);
    listener(structuredClone(this.snapshot));
    return () => this.listeners.delete(listener);
  }

  async connect(profile) {
    const clean = validateConnectProfile(profile);
    this.snapshot.connection.phase = ConnectionPhase.CONNECTING;
    this.#publish();
    try {
      const result = await this.#request("POST", "/join", {
        controller_url: clean.controllerUrl,
        endpoint: clean.endpoint,
        available_memory_mb: clean.availableMemoryMb,
        compute_weight: clean.computeWeight,
        node_name: clean.nodeName,
        platform_name: clean.platformName,
        registration_token: clean.registrationToken,
      });
      this.snapshot.node.nodeId = result.node_id ?? null;
      this.snapshot.connection.phase = ConnectionPhase.CONNECTED;
      this.#publish();
      return structuredClone(this.snapshot);
    } catch (error) {
      this.snapshot.connection.phase = ConnectionPhase.DISCONNECTED;
      this.snapshot.connection.lastError = { source: "connection", code: error.code, message: error.message };
      this.snapshot.errorLog.unshift({ atMs: Date.now(), source: "connection", code: error.code, message: error.message });
      this.#publish();
      throw error;
    }
  }

  async getStatus() {
    const status = await this.#request("GET", "/status");
    this.snapshot = { ...this.snapshot, ...status, adapter: "local-http" };
    assertNoSecrets(this.snapshot);
    this.#publish();
    return structuredClone(this.snapshot);
  }

  async updateResources(patch) {
    const clean = validateResourcePatch(patch);
    const snake = {
      available_memory_mb: clean.availableMemoryMb,
      compute_weight: clean.computeWeight,
      accepting_tasks: clean.acceptingTasks,
    };
    const status = await this.#request("POST", "/resources", snake);
    this.snapshot = { ...this.snapshot, ...status, adapter: "local-http" };
    assertNoSecrets(this.snapshot);
    this.#publish();
    return structuredClone(this.snapshot);
  }

  async requestStopExecution() {
    const result = await this.#request("POST", "/execution/stop", {});
    this.snapshot.execution = result.execution ?? null;
    this.#publish();
    return result;
  }

  async getRecentAssignments() {
    const result = await this.#request("GET", "/assignments");
    return result.assignments ?? [];
  }

  async disconnect() {
    this.snapshot.connection.phase = ConnectionPhase.DISCONNECTED;
    this.#publish();
    return structuredClone(this.snapshot);
  }
}
