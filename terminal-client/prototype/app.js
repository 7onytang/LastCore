import { ConnectionPhase, ErrorCode, NodeAdapterError } from "./adapter/adapter.js";
import { MockNodeAdapter, MockScenario } from "./adapter/mock-adapter.js";
import { LocalHttpNodeAdapter } from "./adapter/local-http-adapter.js";

const $ = (id) => document.getElementById(id);

const state = {
  adapter: null,
  unsubscribe: null,
  snapshot: null,
};

const PHASE_LABEL = {
  [ConnectionPhase.DISCONNECTED]: "未连接",
  [ConnectionPhase.CONNECTING]: "入网中",
  [ConnectionPhase.CONNECTED]: "已连接",
  [ConnectionPhase.OFFLINE]: "离线（心跳丢失）",
};

function formatTime(ms) {
  if (!ms) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(ms));
}

function showError(code, message) {
  $("error-banner").classList.remove("banner-hidden");
  $("error-banner").classList.add("banner-error");
  $("error-code").textContent = code ? `[${code}]` : "";
  $("error-message").textContent = message;
}

function hideError() {
  $("error-banner").classList.add("banner-hidden");
}

function handleAdapterError(error) {
  if (error instanceof NodeAdapterError) {
    showError(error.code, error.message);
  } else {
    showError(ErrorCode.CONTROLLER_ERROR, error?.message ?? String(error));
  }
}

function createMockAdapter() {
  return new MockNodeAdapter();
}

function createLocalHttpAdapter() {
  return new LocalHttpNodeAdapter($("p-local-url").value || "http://127.0.0.1:9090");
}

function setAdapter(adapter) {
  if (state.unsubscribe) {
    state.unsubscribe();
    state.unsubscribe = null;
  }
  state.adapter = adapter;
  state.unsubscribe = adapter.subscribe((snapshot) => {
    state.snapshot = snapshot;
    render(snapshot);
  });
}

function render(snapshot) {
  renderPill(snapshot);
  renderConnect(snapshot);
  renderStatus(snapshot);
  renderAssignments(snapshot);
  renderStop(snapshot);
}

function renderPill(snapshot) {
  const pill = $("conn-pill");
  const phase = snapshot.connection.phase;
  pill.textContent = PHASE_LABEL[phase] ?? phase;
  pill.className = `pill pill-${phase}`;
  if (phase === ConnectionPhase.CONNECTING && snapshot.connection.retryCount > 0) {
    pill.textContent = `入网中（重试 ${snapshot.connection.retryCount}）`;
  }
}

function renderConnect(snapshot) {
  const result = $("connect-result");
  if (snapshot.node.nodeId) {
    result.innerHTML = `
      <div class="ok">已入网：节点 ID <strong>${escapeHtml(snapshot.node.nodeId)}</strong></div>
      <div class="readback">名称 ${escapeHtml(snapshot.node.name)} · 平台 ${escapeHtml(snapshot.node.platform)} · endpoint ${escapeHtml(snapshot.node.endpoint)}</div>
      <div class="readback">凭据由本地 adapter / node-agent 持久化，界面不保存、不显示 token。</div>`;
  }
  $("btn-connect").disabled = snapshot.connection.phase === ConnectionPhase.CONNECTING
    || snapshot.connection.phase === ConnectionPhase.CONNECTED;
}

function renderStatus(snapshot) {
  const { node, connection, execution } = snapshot;
  const cards = [
    card("连接状态", PHASE_LABEL[connection.phase] ?? connection.phase,
      connection.retryCount > 0 ? `重试 ${connection.retryCount} 次` : ""),
    card("节点身份", node.nodeId ?? "-", node.name ? `${node.name} · ${node.platform}` : ""),
    card("心跳", node.heartbeat.lastOkAtMs ? formatTime(node.heartbeat.lastOkAtMs) : "无记录",
      node.heartbeat.failCount > 0 ? `连续失败 ${node.heartbeat.failCount} 次` : "正常"),
    card("资源设置（回读）", `${node.availableMemoryMb} MB · 权重 ${node.computeWeight}`,
      node.acceptingTasks ? "接受新任务" : "排空中（不接新任务）"),
    card("当前执行", execution ? execution.assignmentId : "无",
      execution ? `${execution.modelId} · 层 [${execution.layerStart}, ${execution.layerEnd}) · ${execution.state}` : ""),
    card("Controller", node.controllerUrl || "-", ""),
  ];
  $("status-cards").innerHTML = cards.join("");
}

function card(title, value, sub) {
  return `<div class="card"><h4>${escapeHtml(title)}</h4><div class="value">${escapeHtml(String(value))}</div>${sub ? `<div class="sub">${escapeHtml(sub)}</div>` : ""}</div>`;
}

function renderAssignments(snapshot) {
  const body = $("assignments-body");
  const rows = snapshot.recentAssignments.map((item) => `
    <tr>
      <td>${escapeHtml(item.assignmentId)}</td>
      <td>${escapeHtml(item.taskId)}</td>
      <td>${escapeHtml(item.modelId)}</td>
      <td>[${item.layerStart}, ${item.layerEnd})</td>
      <td class="status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</td>
      <td>${formatTime(item.startedAtMs)}</td>
      <td>${formatTime(item.endedAtMs)}</td>
      <td>${escapeHtml(item.error ?? "")}</td>
    </tr>`);
  body.innerHTML = rows.join("") || `<tr><td colspan="8" style="color:var(--muted)">暂无分配记录</td></tr>`;
  renderErrorLog(snapshot);
}

function renderErrorLog(snapshot) {
  const list = $("error-log");
  const entries = snapshot.errorLog ?? [];
  if (entries.length === 0) {
    list.innerHTML = `<li class="muted">暂无错误记录</li>`;
    return;
  }
  list.innerHTML = entries.map((item) => `
    <li><span class="code">[${escapeHtml(item.code)}]</span> ${escapeHtml(item.message)}
      <span class="muted">${formatTime(item.atMs)}</span></li>`).join("");
  const last = entries[0];
  if (last.code === ErrorCode.AUTH_REJECTED || last.code === ErrorCode.UNREACHABLE
    || last.code === ErrorCode.MODEL_UNSUPPORTED) {
    showError(last.code, last.message);
  }
}

function renderStop(snapshot) {
  const execution = snapshot.execution;
  const stopState = $("stop-state");
  if (execution) {
    stopState.innerHTML = [
      card("执行中分配", execution.assignmentId, `任务 ${execution.taskId}`),
      card("模型与片段", execution.modelId, `层 [${execution.layerStart}, ${execution.layerEnd})`),
      card("执行状态", execution.state, `开始于 ${formatTime(execution.startedAtMs)}`
        + (execution.cancelRequested ? " · 已请求取消" : "")),
    ].join("");
    const stoppable = ["starting", "running"].includes(execution.state);
    $("btn-stop").disabled = !stoppable;
    $("stop-reason").textContent = stoppable
      ? "确认框展示以上执行状态；停止后以 adapter 回报的 stopped 为准。"
      : `当前状态 ${execution.state}，不可重复停止。`;
  } else {
    stopState.innerHTML = card("当前执行", "无", "以执行状态为准：没有可停止的本地任务") ;
    $("btn-stop").disabled = true;
    $("stop-reason").textContent = "当前无执行中任务（取自执行状态），停止按钮不可用。";
  }
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === name);
  });
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `panel-${name}`);
  });
}

async function onConnect(event) {
  event.preventDefault();
  hideError();
  $("connect-form-error").textContent = "";
  const token = $("f-reg-token").value;
  try {
    await state.adapter.connect({
      controllerUrl: $("f-controller-url").value,
      endpoint: $("f-endpoint").value,
      availableMemoryMb: Number($("f-memory").value),
      computeWeight: Number($("f-weight").value),
      registrationToken: token,
      nodeName: $("f-node-name").value,
      platformName: $("f-platform").value,
    });
  } catch (error) {
    if (error instanceof NodeAdapterError && error.code === ErrorCode.INVALID_INPUT) {
      $("connect-form-error").textContent = error.message;
    } else {
      handleAdapterError(error);
    }
  } finally {
    $("f-reg-token").value = "";
  }
}

async function onResources(event) {
  event.preventDefault();
  hideError();
  try {
    const snapshot = await state.adapter.updateResources({
      availableMemoryMb: Number($("r-memory").value),
      computeWeight: Number($("r-weight").value),
      acceptingTasks: $("r-accepting").checked,
    });
    const node = snapshot.node;
    $("resources-result").innerHTML = `
      <div class="ok">已保存并回读实际生效值：</div>
      <div class="readback">内存 ${node.availableMemoryMb} MB · 权重 ${node.computeWeight} · ${node.acceptingTasks ? "接受新任务" : "排空中"}</div>
      <div class="readback">回读时间 ${formatTime(Date.now())}（Asia/Shanghai）</div>`;
  } catch (error) {
    handleAdapterError(error);
  }
}

function onStopClick() {
  hideError();
  const execution = state.snapshot?.execution;
  if (!execution) {
    showError(ErrorCode.INVALID_STATE, "当前没有可停止的执行（以执行状态为准）");
    return;
  }
  $("stop-dialog-details").innerHTML = [
    card("分配", execution.assignmentId, `任务 ${execution.taskId}`),
    card("执行状态", execution.state, `开始于 ${formatTime(execution.startedAtMs)}`),
  ].join("");
  $("stop-dialog").showModal();
}

async function onStopConfirm() {
  $("stop-dialog").close();
  try {
    const result = await state.adapter.requestStopExecution();
    $("stop-reason").textContent = `已请求停止 ${result.stoppedAssignmentId ?? ""}，等待 adapter 回报 stopped。`;
  } catch (error) {
    handleAdapterError(error);
  }
}

function wirePrototypeControls() {
  $("p-adapter").addEventListener("change", (event) => {
    const isLocal = event.target.value === "local-http";
    $("p-local-url-wrap").classList.toggle("hidden", !isLocal);
    setAdapter(isLocal ? createLocalHttpAdapter() : createMockAdapter());
    $("connect-result").innerHTML = "";
    $("resources-result").innerHTML = "";
  });
  $("p-scenario").addEventListener("change", (event) => {
    if (state.adapter instanceof MockNodeAdapter) {
      state.adapter.setScenario(event.target.value);
    } else {
      showError(ErrorCode.INVALID_STATE, "故障场景注入仅适用于 Mock adapter");
    }
  });
  $("p-start-assignment").addEventListener("click", async () => {
    try {
      await state.adapter.mockStartAssignment?.();
    } catch (error) {
      handleAdapterError(error);
    }
  });
  $("p-complete-assignment").addEventListener("click", async () => {
    try {
      await state.adapter.mockCompleteAssignment?.();
    } catch (error) {
      handleAdapterError(error);
    }
  });
  $("p-heartbeat-down").addEventListener("click", () => {
    state.adapter.setHeartbeatDown?.(true);
    if (!(state.adapter instanceof MockNodeAdapter)) {
      showError(ErrorCode.INVALID_STATE, "心跳注入仅适用于 Mock adapter");
    }
  });
  $("p-heartbeat-up").addEventListener("click", () => {
    state.adapter.setHeartbeatDown?.(false);
  });
}

function wire() {
  $("tabs").addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (tab) switchTab(tab.dataset.tab);
  });
  $("connect-form").addEventListener("submit", onConnect);
  $("resources-form").addEventListener("submit", onResources);
  $("btn-stop").addEventListener("click", onStopClick);
  $("btn-stop-cancel").addEventListener("click", () => $("stop-dialog").close());
  $("btn-stop-confirm").addEventListener("click", onStopConfirm);
  $("error-dismiss").addEventListener("click", hideError);
  wirePrototypeControls();
  setAdapter(createMockAdapter());
}

wire();
