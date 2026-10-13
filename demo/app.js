/* Local replay of published historical evidence. No task mutation or credential input. */
(() => {
  "use strict";
  const evidence = window.LASTCORE_EVIDENCE;
  if (!evidence || !evidence.cases) return;
  const byId = (id) => document.getElementById(id);
  const stepList = byId("replay-steps"), panel = byId("replay-panel");
  const playButton = byId("play-replay"), reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let caseId = "generation", stepIndex = 3, timer = null;
  const dateFormat = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
  const setText = (id, text) => {byId(id).textContent = text;};
  function pause() {
    if (timer !== null) window.clearInterval(timer);
    timer = null;
    playButton.querySelector("span").textContent = "播放回放";
    playButton.setAttribute("aria-pressed", "false");
    playButton.querySelector("svg").style.opacity = "1";
  }
  function render() {
    const record = evidence.cases[caseId], current = record.steps[stepIndex];
    const final = stepIndex === record.steps.length - 1;
    stepList.replaceChildren();
    record.steps.forEach((step, index) => {
      const li = document.createElement("li"), button = document.createElement("button"), circle = document.createElement("span"), label = document.createElement("span"), small = document.createElement("small");
      button.type = "button"; button.className = index === stepIndex ? "active" : "";
      button.setAttribute("aria-current", index === stepIndex ? "step" : "false");
      button.setAttribute("aria-label", `第 ${index + 1} 步：${step.title}`);
      circle.className = "step-circle"; circle.textContent = String(index + 1).padStart(2, "0");
      label.append(document.createTextNode(step.title)); small.textContent = step.state; label.append(small);
      button.append(circle, label); button.addEventListener("click", () => {pause();stepIndex = index;render();});
      li.append(button); stepList.append(li);
    });
    document.querySelectorAll("[data-case]").forEach((button) => button.setAttribute("aria-selected", String(button.dataset.case === caseId)));
    panel.setAttribute("aria-labelledby", `tab-${caseId}`);
    setText("replay-platform", record.platform);
    setText("replay-state", current.state);
    setText("replay-prompt", record.prompt);
    setText("result-label", final ? record.resultLabel : "记录中的阶段");
    setText("replay-result", final ? record.displayResult : current.display);
    setText("replay-quote", final ? record.result : current.description);
    setText("replay-receipt", final ? "stop acknowledged = true" : "尚未到停止确认步骤");
    setText("replay-elapsed", final && record.elapsedSeconds !== null ? `elapsed ${record.elapsedSeconds} s` : "");
    setText("replay-stage-note", current.note);
    setText("replay-time", `${record.timeLabel} ${dateFormat.format(new Date(record.recordedAt))} · 北京时间`);
    setText("replay-source", `${record.sourceLabel} ${record.sourceCommit.slice(0, 7)} · case ${record.caseId}`);
    byId("evidence-link").href = record.evidenceUrl;
  }
  function selectCase(id) {if (!evidence.cases[id]) return; pause();caseId = id;stepIndex = evidence.cases[id].steps.length - 1;render();}
  document.querySelectorAll("[data-case]").forEach((button) => {
    if (!evidence.cases[button.dataset.case]) {button.disabled = true;return;}
    button.addEventListener("click", () => selectCase(button.dataset.case));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const tabs = [...document.querySelectorAll("[data-case]")].filter((tab) => !tab.disabled);
      const current = tabs.indexOf(button), next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      selectCase(tabs[next].dataset.case);tabs[next].focus();
    });
  });
  playButton.addEventListener("click", () => {
    if (timer !== null) {pause();return;}
    if (reducedMotion.matches) {stepIndex = (stepIndex + 1) % evidence.cases[caseId].steps.length;render();setText("replay-stage-note", `${evidence.cases[caseId].steps[stepIndex].note} 已遵循减少动态效果设置，改为逐步前进。`);return;}
    if (stepIndex === evidence.cases[caseId].steps.length - 1) stepIndex = 0;
    render(); playButton.querySelector("span").textContent = "暂停回放";playButton.setAttribute("aria-pressed", "true");
    timer = window.setInterval(() => {
      stepIndex += 1; render();
      if (stepIndex >= evidence.cases[caseId].steps.length - 1) pause();
    }, 1400);
  });
  byId("reset-replay").addEventListener("click", () => {pause();stepIndex = 0;render();});
  document.addEventListener("visibilitychange", () => {if(document.hidden) pause();});
  reducedMotion.addEventListener("change", pause);
  let checking = false;
  byId("check-health").addEventListener("click", async () => {
    if (checking) return;
    checking = true; byId("check-health").disabled = true;
    const signal = new AbortController(), timeout = window.setTimeout(() => signal.abort(), 5000);
    setText("health-state", "检查中 · 仅请求公开 /healthz");
    try {
      const response = await fetch("https://8.134.161.123/healthz", {method: "GET", credentials: "omit", redirect: "error", cache: "no-store", signal: signal.signal});
      if (!response.ok) throw new Error("unconfirmed");
      const body = await response.json();if(body.status !== "ok") throw new Error("unconfirmed");
      setText("health-state", `API 可达 · ${dateFormat.format(new Date())} 北京时间 · 不代表 Worker 在线`);
    } catch (_) {setText("health-state", "未确认 API 可达 · 可能离线或受浏览器跨域限制");}
    finally {window.clearTimeout(timeout);checking = false;byId("check-health").disabled = false;}
  });
  window.addEventListener("offline", () => {setText("health-state", "已离线 · 此页仍可查看历史记录");});
  render();
})();
