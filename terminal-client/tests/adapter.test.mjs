import assert from "node:assert/strict";
import test from "node:test";

import {
  ConnectionPhase,
  ErrorCode,
  ExecutionState,
  NodeAdapterError,
  assertNoSecrets,
} from "../prototype/adapter/adapter.js";
import { MockNodeAdapter, MockScenario } from "../prototype/adapter/mock-adapter.js";
import { LocalHttpNodeAdapter } from "../prototype/adapter/local-http-adapter.js";

const TOKEN = "super-secret-registration-token-abc123";

function makeAdapter(extra = {}) {
  return new MockNodeAdapter({
    connectLatencyMs: 5,
    heartbeatMs: 10,
    retryMs: 5,
    assignmentStartMs: 5,
    stopGraceMs: 10,
    ...extra,
  });
}

function profile() {
  return {
    controllerUrl: "http://127.0.0.1:8000",
    endpoint: "http://127.0.0.1:9000",
    availableMemoryMb: 8192,
    computeWeight: 3,
    registrationToken: TOKEN,
    nodeName: "test-node",
    platformName: "test-platform",
  };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test("connect 成功：快照含 node_id，无任何秘密字段或 token 值", async () => {
  const adapter = makeAdapter();
  const seen = [];
  adapter.subscribe((snapshot) => seen.push(snapshot));
  const snapshot = await adapter.connect(profile());
  assert.ok(snapshot.node.nodeId);
  assert.equal(snapshot.connection.phase, ConnectionPhase.CONNECTED);
  assert.equal(snapshot.node.availableMemoryMb, 8192);
  assert.equal(snapshot.node.computeWeight, 3);
  assert.equal(snapshot.node.heartbeat.lastOkAtMs !== null, true);
  const serialized = JSON.stringify(seen) + JSON.stringify(snapshot);
  assert.equal(serialized.includes(TOKEN), false);
  assert.equal(serialized.toLowerCase().includes("registrationtoken"), false);
  await adapter.disconnect();
});

test("connect 校验：非法输入返回 INVALID_INPUT 且逐条中文原因", async () => {
  const adapter = makeAdapter();
  await assert.rejects(
    adapter.connect({ ...profile(), controllerUrl: "not-a-url", availableMemoryMb: -1 }),
    (error) => {
      assert.equal(error.code, ErrorCode.INVALID_INPUT);
      assert.equal(error.details.errors.length, 2);
      return true;
    },
  );
});

test("凭据被拒（401 语义）：显式 AUTH_REJECTED、不自动重试、界面可见原因", async () => {
  const adapter = makeAdapter({ scenario: MockScenario.AUTH_REJECTED });
  const seen = [];
  adapter.subscribe((snapshot) => seen.push(snapshot));
  await assert.rejects(adapter.connect(profile()), (error) => error.code === ErrorCode.AUTH_REJECTED);
  const snapshot = await adapter.getStatus();
  assert.equal(snapshot.connection.phase, ConnectionPhase.DISCONNECTED);
  assert.equal(snapshot.connection.lastError.code, ErrorCode.AUTH_REJECTED);
  assert.match(snapshot.connection.lastError.message, /不自动重新注册/);
  await sleep(30);
  const later = await adapter.getStatus();
  assert.equal(later.connection.retryCount, 0);
  const serialized = JSON.stringify(seen);
  assert.equal(serialized.includes(TOKEN), false);
});

test("Controller 不可达：保持 connecting 并累计重试次数，场景恢复后可入网", async () => {
  const adapter = makeAdapter({ scenario: MockScenario.CONTROLLER_DOWN });
  const pending = adapter.connect(profile());
  await sleep(60);
  const during = await adapter.getStatus();
  assert.equal(during.connection.phase, ConnectionPhase.CONNECTING);
  assert.ok(during.connection.retryCount >= 2);
  assert.equal(during.connection.lastError.code, ErrorCode.UNREACHABLE);
  adapter.setScenario(MockScenario.NORMAL);
  await pending;
  const snapshot = await adapter.getStatus();
  assert.equal(snapshot.connection.phase, ConnectionPhase.CONNECTED);
  await adapter.disconnect();
});

test("资源设置：保存后回读生效值，未连接时 INVALID_STATE，非法值 INVALID_INPUT", async () => {
  const adapter = makeAdapter();
  await assert.rejects(
    adapter.updateResources({ availableMemoryMb: 4096 }),
    (error) => error.code === ErrorCode.INVALID_STATE,
  );
  await adapter.connect(profile());
  const snapshot = await adapter.updateResources({
    availableMemoryMb: 4096,
    computeWeight: 1.5,
    acceptingTasks: false,
  });
  assert.equal(snapshot.node.availableMemoryMb, 4096);
  assert.equal(snapshot.node.computeWeight, 1.5);
  assert.equal(snapshot.node.acceptingTasks, false);
  await assert.rejects(
    adapter.updateResources({ availableMemoryMb: 0 }),
    (error) => error.code === ErrorCode.INVALID_INPUT,
  );
  await assert.rejects(
    adapter.updateResources({}),
    (error) => error.code === ErrorCode.INVALID_INPUT,
  );
  await adapter.disconnect();
});

test("停止：无执行时 INVALID_STATE；有执行时经 stopping 到 stopped 且列表更新", async () => {
  const adapter = makeAdapter();
  await adapter.connect(profile());
  await assert.rejects(
    adapter.requestStopExecution(),
    (error) => {
      assert.equal(error.code, ErrorCode.INVALID_STATE);
      assert.match(error.message, /以执行状态为准/);
      return true;
    },
  );
  await adapter.mockStartAssignment();
  let snapshot = await adapter.getStatus();
  assert.equal(snapshot.execution.state, ExecutionState.RUNNING);
  const result = await adapter.requestStopExecution();
  assert.equal(result.stoppedAssignmentId, snapshot.execution.assignmentId);
  snapshot = await adapter.getStatus();
  assert.equal(snapshot.execution, null);
  const entry = snapshot.recentAssignments.find(
    (item) => item.assignmentId === result.stoppedAssignmentId,
  );
  assert.equal(entry.status, ExecutionState.STOPPED);
  await assert.rejects(
    adapter.requestStopExecution(),
    (error) => error.code === ErrorCode.INVALID_STATE,
  );
  await adapter.disconnect();
});

test("模型不支持：执行失败并带 MODEL_UNSUPPORTED 显式原因，不假成功", async () => {
  const adapter = makeAdapter({ scenario: MockScenario.MODEL_UNSUPPORTED });
  await adapter.connect(profile());
  await adapter.mockStartAssignment();
  const snapshot = await adapter.getStatus();
  assert.equal(snapshot.execution, null);
  const entry = snapshot.recentAssignments[0];
  assert.equal(entry.status, ExecutionState.FAILED);
  assert.match(entry.error, /模型不支持/);
  assert.equal(
    snapshot.errorLog.some((item) => item.code === ErrorCode.MODEL_UNSUPPORTED),
    true,
  );
  await adapter.disconnect();
});

test("心跳断链：连续失败后转 offline，恢复后回 connected；全程不泄漏 token", async () => {
  const adapter = makeAdapter();
  const seen = [];
  adapter.subscribe((snapshot) => seen.push(snapshot));
  await adapter.connect(profile());
  adapter.setHeartbeatDown(true);
  await sleep(60);
  let snapshot = await adapter.getStatus();
  assert.equal(snapshot.connection.phase, ConnectionPhase.OFFLINE);
  assert.ok(snapshot.node.heartbeat.failCount >= 3);
  adapter.setHeartbeatDown(false);
  await sleep(30);
  snapshot = await adapter.getStatus();
  assert.equal(snapshot.connection.phase, ConnectionPhase.CONNECTED);
  assert.equal(snapshot.node.heartbeat.failCount, 0);
  const serialized = JSON.stringify(seen);
  assert.equal(serialized.includes(TOKEN), false);
  await adapter.disconnect();
});

test("assertNoSecrets：快照结构携带秘密字段名时抛错", () => {
  assert.throws(
    () => assertNoSecrets({ node: { nodeId: "x", nodeToken: "y" } }),
    (error) => error instanceof NodeAdapterError,
  );
  assert.doesNotThrow(() => assertNoSecrets({ node: { nodeId: "x", name: "n" } }));
});

test("LocalHttpNodeAdapter：本地 API 缺失时显式 LOCAL_API_UNAVAILABLE", async () => {
  const adapter = new LocalHttpNodeAdapter("http://127.0.0.1:9", { timeoutMs: 300 });
  await assert.rejects(
    adapter.getStatus(),
    (error) => error.code === ErrorCode.LOCAL_API_UNAVAILABLE,
  );
  await assert.rejects(
    adapter.connect(profile()),
    (error) => error.code === ErrorCode.LOCAL_API_UNAVAILABLE,
  );
});

test("LocalHttpNodeAdapter：回环凭据被拒映射为 AUTH_REJECTED", async () => {
  const { createServer } = await import("node:http");
  const server = createServer((request, response) => {
    response.statusCode = 401;
    response.setHeader("Content-Type", "application/json");
    response.end(JSON.stringify({ detail: "loopback token rejected" }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  const adapter = new LocalHttpNodeAdapter(`http://127.0.0.1:${port}`, { timeoutMs: 1000 });
  try {
    await assert.rejects(
      adapter.connect(profile()),
      (error) => {
        assert.equal(error.code, ErrorCode.AUTH_REJECTED);
        return true;
      },
    );
    const snapshot = adapter.snapshot;
    assert.equal(snapshot.connection.phase, ConnectionPhase.DISCONNECTED);
    assert.equal(snapshot.connection.lastError.code, ErrorCode.AUTH_REJECTED);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
