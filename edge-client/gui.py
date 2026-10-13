"""Local device-only UI; no fleet listing or admin task controls."""
from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

from linear_edge import Session, VERSION


def main():
    root = tk.Tk()
    root.title(f"LastCore Edge {VERSION} · 本机线性任务")
    root.geometry("720x430")
    file = tk.StringVar()
    verified_origin = tk.StringVar(value="https://8.134.161.123")
    status = tk.StringVar(value="未连接")
    node = tk.StringVar(value="未加载")
    task = tk.StringVar(value="空闲")
    result = tk.StringVar(value="尚无云端确认的计算结果")
    state = {"session": None, "thread": None, "error": None}
    panel = ttk.Frame(root, padding=20)
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text="本机任务与连接状态", font=("", 16)).pack(anchor="w")
    ttk.Label(panel, text="真实 tanh(Wx+b) · 保留现有节点身份 · 活动时间为北京时间").pack(anchor="w", pady=8)
    line = ttk.Frame(panel)
    line.pack(fill="x", pady=8)
    ttk.Entry(line, textvariable=file).pack(side="left", fill="x", expand=True)
    ttk.Button(line, text="选择现有节点配置", command=lambda: file.set(filedialog.askopenfilename())).pack(side="right")
    ttk.Label(panel, text="确认原节点凭据的签发源（HTTPS）").pack(anchor="w")
    ttk.Entry(panel, textvariable=verified_origin).pack(fill="x", pady=6)
    for label, variable in (("连接状态", status), ("本机节点 ID", node), ("当前/最近任务", task), ("实际结果", result)):
        row = ttk.Frame(panel)
        row.pack(fill="x", pady=5)
        ttk.Label(row, text=label + "：", width=16).pack(side="left")
        ttk.Label(row, textvariable=variable, wraplength=510).pack(side="left")

    def work(session):
        try:
            session.agent.run_forever()
        except Exception as exc:
            state["error"] = type(exc).__name__
        finally:
            session.close()

    def start():
        if state["thread"] is not None and state["thread"].is_alive():
            return
        try:
            session = Session(file.get(), verified_origin.get())
        except Exception as exc:
            status.set("启动失败：" + type(exc).__name__ + "；请核对配置/状态目录/原 HTTPS 源")
            return
        state.update(session=session, error=None)
        state["thread"] = threading.Thread(target=work, args=(session,), daemon=True)
        state["thread"].start()
        node.set(session.node_id)
        status.set("连接中，等待真实心跳")

    def stop():
        session = state["session"]
        if session is not None:
            session.agent.shutdown()
        status.set("正在停止本机任务")

    def refresh():
        session, thread = state["session"], state["thread"]
        if session is not None and thread is not None:
            alive = thread.is_alive()
            age = time.monotonic() - session.client.last_heartbeat
            status.set("已停止" if not alive else "已连接 · 接收线性任务" if age < 25 else "连接中/心跳中断")
            if state["error"]:
                status.set("已停止：" + state["error"])
            run = session.agent._current_run()
            task.set(run.task_id if run else session.client.last_task or "空闲")
            if session.client.last_result is not None:
                result.set(", ".join(f"{v:.8f}" for v in session.client.last_result[:16]))
        root.after(500, refresh)

    buttons = ttk.Frame(panel)
    buttons.pack(fill="x", pady=14)
    ttk.Button(buttons, text="连接并接收任务", command=start).pack(side="left")
    ttk.Button(buttons, text="停止本机执行", command=stop).pack(side="left", padx=10)

    def close():
        stop()
        if state["thread"] is not None and state["thread"].is_alive():
            status.set("等待本机停止确认")
            root.after(200, close)
        else:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
