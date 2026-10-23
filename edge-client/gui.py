"""Local device-only UI; no fleet listing or admin task controls."""
from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

from linear_edge import Session, VERSION
from view_state import device_status, task_result_labels, NO_CONFIRMED_RESULT, admission_text, recent_error_text


def main():
    root = tk.Tk()
    root.title(f"LastCore Edge {VERSION} · 本机线性任务")
    root.geometry("720x600")
    file = tk.StringVar()
    verified_origin = tk.StringVar(value="https://8.134.161.123")
    status = tk.StringVar(value="未连接")
    node = tk.StringVar(value="未加载")
    task = tk.StringVar(value="空闲")
    result = tk.StringVar(value=NO_CONFIRMED_RESULT)
    actual_origin = tk.StringVar(value="尚未加载会话")
    resources = tk.StringVar(value="配置资源尚未加载")
    admission = tk.StringVar(value="未运行")
    recent_errors = tk.StringVar(value="暂无已观察错误")
    state = {"session": None, "thread": None, "error": None, "stopping": False}
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
    for label, variable in (("连接状态", status), ("本机节点 ID", node), ("当前/最近任务", task), ("最近已确认结果", result), ("实际 Controller", actual_origin), ("资源摘要", resources), ("接收新任务", admission), ("近期错误", recent_errors)):
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
            if not session.close():
                state['stopping'] = True

    def start():
        if state["thread"] is not None and state["thread"].is_alive():
            return
        previous = state['session']
        if previous is not None and not previous.close():
            state['stopping'] = True
            status.set('停止未确认 · 工作线程仍在运行')
            return
        try:
            session = Session(file.get(), verified_origin.get())
        except Exception as exc:
            status.set("启动失败：" + type(exc).__name__ + "；请核对配置/状态目录/原 HTTPS 源")
            return
        state.update(session=session, error=None, stopping=False)
        task.set("空闲")
        result.set(NO_CONFIRMED_RESULT)
        state["thread"] = threading.Thread(target=work, args=(session,), daemon=True)
        state["thread"].start()
        node.set(session.node_id)
        status.set("连接中，等待真实心跳")

    def stop():
        session = state["session"]
        thread = state['thread']
        if session is None or (not session.has_live_execution() and (thread is None or not thread.is_alive())):
            if session is not None: session.close()
            state['stopping'] = False
            status.set('已停止')
            admission.set('未运行')
            return
        state["stopping"] = True
        session.agent.shutdown()
        status.set("正在停止本机任务")

    def set_accepting(value):
        session, thread = state['session'], state['thread']
        if session is None or thread is None or not thread.is_alive() or state['stopping']:
            return
        try:
            session.set_accepting_tasks(value)
            admission.set('恢复接单待确认' if value else '暂停接单待确认')
        except (ValueError, OSError):
            admission.set('控制请求未完成，请重试')

    def pause():
        set_accepting(False)

    def resume():
        set_accepting(True)

    def refresh():
        session, thread = state["session"], state["thread"]
        if session is not None and thread is not None:
            loop_alive = thread.is_alive()
            snapshot = session.status_snapshot()
            execution_alive = snapshot['execution_alive']
            alive = loop_alive or execution_alive
            if not loop_alive:
                state['stopping'] = execution_alive
                if not execution_alive: session.close()
            age = time.monotonic() - session.client.last_heartbeat
            if not loop_alive and execution_alive:
                status.set('停止未确认 · 工作线程仍在运行')
            else:
                status.set(device_status(alive,age,session.config.accepting_tasks,state['stopping'],
                                         state['error'],session.client.recovery_required, snapshot.get('admission'),
                                         session.admission_request_id, snapshot.get('connection')))
            actual_origin.set(snapshot['controller_origin'])
            resources.set(f"配置上报内存 {snapshot['available_memory_mb']} MiB · 权重 {snapshot['compute_weight']} · {snapshot['cpu_logical'] or 'Unavailable'} 逻辑核心（非占用测量）")
            admission.set(admission_text(snapshot.get('admission'), session.admission_request_id, alive, state['stopping']))
            recent_errors.set(recent_error_text(snapshot.get('recent_errors', [])))
            run = session.agent._current_run()
            confirmed = session.client.acknowledged_result
            completed_task, completed_values = confirmed if confirmed is not None else (None, None)
            task_text, result_text = task_result_labels(
                run.task_id if run and execution_alive else None, completed_task, completed_values)
            task.set(task_text)
            result.set(result_text)
        else:
            task.set("空闲")
            result.set(NO_CONFIRMED_RESULT)
            admission.set('未运行')
        root.after(500, refresh)

    buttons = ttk.Frame(panel)
    buttons.pack(fill="x", pady=14)
    ttk.Button(buttons, text="连接并接收任务", command=start).pack(side="left")
    ttk.Button(buttons, text="停止本机执行", command=stop).pack(side="left", padx=10)
    ttk.Button(buttons, text="暂停接单", command=pause).pack(side="left", padx=5)
    ttk.Button(buttons, text="恢复接单", command=resume).pack(side="left", padx=5)

    def close():
        stop()
        session = state['session']
        if ((state["thread"] is not None and state["thread"].is_alive())
                or (session is not None and not session.close())):
            status.set("等待本机停止确认")
            root.after(200, close)
        else:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    refresh()
    root.mainloop()


if __name__ == "__main__":
    main()
