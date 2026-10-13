"""Pure device state labels; importing this module never launches a GUI."""


def device_status(alive, heartbeat_age, accepting_tasks, stopping, error=None, recovery=False):
    if recovery:return "结果恢复待确认 · 待回报记录已保留"
    if error:return "已停止：" + error
    if not alive:return "已停止"
    if stopping:return "正在停止本机任务"
    if heartbeat_age>=25:return "连接中/心跳中断"
    return "已连接 · 接收线性任务" if accepting_tasks else "已连接 · 暂停接单"
