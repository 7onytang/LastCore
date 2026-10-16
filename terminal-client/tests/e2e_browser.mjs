import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { mkdtempSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, extname } from "node:path";
import { fileURLToPath } from "node:url";

const PROTOTYPE_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "prototype");
const CHROME_CANDIDATES = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  process.env.PROGRAMFILES ? join(process.env.PROGRAMFILES, "Google/Chrome/Application/chrome.exe") : null,
  process.env["PROGRAMFILES(X86)"] ? join(process.env["PROGRAMFILES(X86)"], "Google/Chrome/Application/chrome.exe") : null,
  process.env.LOCALAPPDATA ? join(process.env.LOCALAPPDATA, "Google/Chrome/Application/chrome.exe") : null,
].filter(Boolean);

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
};

function startStaticServer(port = 0) {
  return new Promise((resolve) => {
    const server = createServer(async (request, response) => {
      const url = new URL(request.url, "http://localhost");
      const relative = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/+/, "");
      const filePath = join(PROTOTYPE_DIR, relative);
      try {
        const body = await readFile(filePath);
        response.setHeader("Content-Type", MIME[extname(filePath)] ?? "application/octet-stream");
        response.end(body);
      } catch {
        response.statusCode = 404;
        response.end("not found");
      }
    });
    server.listen(port, "127.0.0.1", () => resolve(server));
  });
}

function findChrome() {
  const override = process.env.E2E_CHROME_PATH;
  const candidates = override ? [override] : CHROME_CANDIDATES;
  for (const candidate of candidates) {
    if (candidate && existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

function launchChrome() {
  const chromePath = findChrome();
  if (!chromePath) {
    return Promise.resolve({ status: "missing" });
  }
  const userDir = mkdtempSync(join(tmpdir(), "lc14-chrome-"));
  let child;
  try {
    child = spawn(chromePath, [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--user-data-dir=" + userDir,
      "--remote-debugging-port=0",
      "about:blank",
    ], { stdio: ["ignore", "ignore", "pipe"] });
  } catch (error) {
    try { rmSync(userDir, { recursive: true, force: true }); } catch {}
    return Promise.resolve({ status: "failed", reason: `chrome failed to spawn: ${error.message}` });
  }
  return new Promise((resolve) => {
    let settled = false;
    const cleanup = () => {
      try { child.kill("SIGKILL"); } catch {}
      try { rmSync(userDir, { recursive: true, force: true }); } catch {}
    };
    const fail = (reason) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      cleanup();
      resolve({ status: "failed", reason });
    };
    const timer = setTimeout(() => fail("chrome devtools endpoint timeout after 15s"), 15000);
    child.stderr.on("data", (chunk) => {
      const match = String(chunk).match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)\//);
      if (match && !settled) {
        settled = true;
        clearTimeout(timer);
        resolve({ status: "ready", child, userDir, port: Number(match[1]) });
      }
    });
    child.on("exit", (code, signal) => fail(`chrome exited before devtools was ready (code=${code}, signal=${signal})`));
    child.on("error", (error) => fail(`chrome failed to spawn: ${error.message}`));
  });
}

class Cdp {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.nextId = 1;
    this.pending = new Map();
    this.ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) {
          reject(new Error(message.error.message));
        } else {
          resolve(message.result);
        }
      }
    });
  }

  static async connect(port) {
    const list = await fetch(`http://127.0.0.1:${port}/json/list`).then((r) => r.json());
    const page = list.find((target) => target.type === "page");
    const cdp = new Cdp(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      cdp.ws.addEventListener("open", resolve, { once: true });
      cdp.ws.addEventListener("error", reject, { once: true });
    });
    return cdp;
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error("page exception: " + JSON.stringify(result.exceptionDetails.exception?.description ?? result.exceptionDetails));
    }
    return result.result.value;
  }

  close() {
    this.ws.close();
  }
}

const FLOW = `(async () => {
  const $ = (id) => document.getElementById(id);
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const TOKEN = "e2e-secret-reg-token-xyz";
  const out = {};

  $("f-endpoint").value = "http://127.0.0.1:9000";
  $("f-memory").value = "8192";
  $("f-weight").value = "3";
  $("f-reg-token").value = TOKEN;
  $("connect-form").requestSubmit();
  await wait(400);

  out.pill = $("conn-pill").textContent;
  out.tokenCleared = $("f-reg-token").value === "";
  out.connectResult = $("connect-result").textContent;
  out.nodeIdShown = out.connectResult.includes("node-mock-");

  document.querySelector('.tab[data-tab="resources"]').click();
  $("r-memory").value = "4096";
  $("r-weight").value = "1.5";
  $("r-accepting").checked = false;
  $("resources-form").requestSubmit();
  await wait(300);
  out.resourceReadback = $("resources-result").textContent;

  document.querySelector('.tab[data-tab="stop"]').click();
  $("p-start-assignment").click();
  await wait(200);
  out.executionBeforeStop = $("stop-state").textContent;
  out.stopEnabledBefore = !$("btn-stop").disabled;
  $("btn-stop").click();
  await wait(50);
  out.dialogShown = $("stop-dialog").open;
  out.dialogDetails = $("stop-dialog-details").textContent;
  $("btn-stop-confirm").click();
  await wait(400);
  out.executionAfterStop = $("stop-state").textContent;
  document.querySelector('.tab[data-tab="results"]').click();
  out.assignmentsText = $("assignments-body").textContent;
  out.stopRowShown = out.assignmentsText.includes("stopped");
  out.stopAgainBlocked = $("btn-stop").disabled;

  $("p-adapter").value = "local-http";
  $("p-adapter").dispatchEvent(new Event("change"));
  $("p-adapter").value = "mock";
  $("p-adapter").dispatchEvent(new Event("change"));
  $("p-scenario").value = "auth_rejected";
  $("p-scenario").dispatchEvent(new Event("change"));
  $("f-reg-token").value = TOKEN;
  $("connect-form").requestSubmit();
  await wait(400);
  out.authBanner = $("error-banner").classList.contains("banner-error") ? $("error-banner").textContent : "";
  out.authPill = $("conn-pill").textContent;
  out.authTokenCleared = $("f-reg-token").value === "";

  out.tokenLeakedIntoDom = document.documentElement.outerHTML.includes(TOKEN);
  out.errorLogShown = $("panel-results").textContent.includes("[AUTH_REJECTED]");
  return out;
})()`;

const checks = {
  已连接: (out) => out.pill === "已连接",
  "token提交后从界面清除": (out) => out.tokenCleared && out.authTokenCleared,
  显示节点ID: (out) => out.nodeIdShown,
  资源保存后回读生效值: (out) => out.resourceReadback.includes("4096") && out.resourceReadback.includes("1.5") && out.resourceReadback.includes("排空"),
  停止按钮随执行状态启用: (out) => out.stopEnabledBefore,
  停止确认展示真实执行状态: (out) => out.dialogShown && out.dialogDetails.includes("running") || out.dialogDetails.includes("starting"),
  "停止后以stopped为准": (out) => out.stopRowShown && out.executionAfterStop.includes("无可停止") === false,
  停止后按钮回到禁用: (out) => out.stopAgainBlocked,
  "401显式报错且不自动重试": (out) => out.authBanner.includes("AUTH_REJECTED") && out.authPill === "未连接",
  token不泄漏进DOM: (out) => out.tokenLeakedIntoDom === false,
  错误日志可见: (out) => out.errorLogShown,
};

async function main() {
  const server = await startStaticServer();
  const httpPort = server.address().port;
  let chrome;
  try {
    const launch = await launchChrome();
    if (launch.status === "missing") {
      if (process.env.E2E_REQUIRE_CHROME === "1") {
        console.error("E2E FAILED: no Chrome/Chromium found and E2E_REQUIRE_CHROME=1");
        process.exitCode = 1;
        return;
      }
      console.log("SKIP: no Chrome available (set E2E_REQUIRE_CHROME=1 to require)");
      return;
    }
    if (launch.status === "failed") {
      console.error("E2E FAILED: " + launch.reason);
      process.exitCode = 1;
      return;
    }
    chrome = launch;
    await new Promise((resolve) => setTimeout(resolve, 500));
    const cdp = await Cdp.connect(chrome.port);
    try {
      await cdp.send("Page.enable");
      await cdp.send("Page.navigate", { url: `http://127.0.0.1:${httpPort}/index.html` });
      await new Promise((resolve) => setTimeout(resolve, 1500));
      const out = await cdp.evaluate(FLOW);
      let failed = 0;
      for (const [name, check] of Object.entries(checks)) {
        let ok;
        try {
          ok = Boolean(check(out));
        } catch {
          ok = false;
        }
        console.log(`${ok ? "PASS" : "FAIL"} ${name}`);
        if (!ok) {
          failed += 1;
        }
      }
      if (failed > 0 || !out.pill) {
        console.log("DETAILS " + JSON.stringify(out, null, 2));
      }
      console.log(failed === 0 ? "E2E OK" : `E2E FAILED: ${failed}`);
      process.exitCode = failed === 0 ? 0 : 1;
    } finally {
      cdp.close();
    }
  } finally {
    if (chrome) {
      try { chrome.child.kill("SIGTERM"); } catch {}
      await new Promise((resolve) => setTimeout(resolve, 300));
      try { chrome.child.kill("SIGKILL"); } catch {}
      try { rmSync(chrome.userDir, { recursive: true, force: true }); } catch {}
    }
    server.close();
  }
}

main().catch((error) => {
  console.error("E2E ERROR", error);
  process.exitCode = 1;
});
