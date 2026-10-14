"""Pure device state labels; importing this module never launches a GUI."""


def device_status(alive, heartbeat_age, accepting_tasks, stopping, error=None, recovery=False):
    if recovery:return "结果恢复待确认 · 待回报记录已保留"
    if error:return "已停止：" + error
    if not alive:return "已停止"
    if stopping:return "正在停止本机任务"
    if heartbeat_age>=25:return "连接中/心跳中断"
    return "已连接 · 接收线性任务" if accepting_tasks else "已连接 · 暂停接单"


NO_CONFIRMED_RESULT = "尚无云端确认的计算结果"


def task_result_labels(active_task_id, completed_task_id, completed_result):
    """Keep running-task identity separate from the last acknowledged result."""
    task = active_task_id or completed_task_id or "空闲"
    if not completed_task_id or completed_result is None or not completed_result:
        return task, NO_CONFIRMED_RESULT
    values = ", ".join(f"{value:.8f}" for value in completed_result[:16])
    if len(completed_result) > 16:
        values += f"（显示前16/{len(completed_result)}项）"
    return task, f"已确认任务 {completed_task_id}：{values}"
