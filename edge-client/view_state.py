"""Pure device state labels; importing this module never launches a GUI."""


def device_status(alive, heartbeat_age, accepting_tasks, stopping, error=None, recovery=False, admission=None, expected_request_id=None, connection=None):
    if recovery:return "结果恢复待确认 · 待回报记录已保留"
    if error:return "已停止：" + error
    if not alive:return "已停止"
    if stopping:return "正在停止本机任务"
    if heartbeat_age>=25:return "连接中/心跳中断"
    if connection in {'unreachable','heartbeat-failed','rejected','recovery-required'}:
        return '连接未确认 · ' + ERROR_LABELS[connection]
    if admission is not None:
        return '已连接 · ' + admission_text(admission, expected_request_id)
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


ERROR_LABELS = {'poll-unavailable':'任务轮询不可达','poll-rejected':'任务轮询被拒绝','unreachable':'Controller 不可达','heartbeat-failed':'心跳失败',
    'heartbeat-stale':'心跳过期','rejected':'凭据被拒绝','recovery-required':'恢复待确认',
    'deadline':'任务超时','heartbeat-stall':'心跳中断','execution_failed':'本机执行失败',
    'executor_unavailable':'执行器不可用','cleanup_timeout':'停止未确认','invalid-command':'控制命令无效'}


def admission_text(value, expected_request_id=None, running=True, stopping=False):
    if not running:
        return '未运行'
    if stopping:
        return '停止中，接单状态未确认'
    if not isinstance(value, dict) or type(value.get('desired')) is not bool:
        return '接单状态未确认'
    matches = expected_request_id is None or value.get('request_id') == expected_request_id
    confirmed = (matches and value.get('status') == 'confirmed'
        and value.get('confirmed') is value['desired']
        and value.get('request_id') == value.get('confirmed_request_id'))
    if confirmed:
        return '接收新任务 · 心跳已确认' if value['desired'] else '暂停接单 · 心跳已确认'
    return '恢复接单待确认' if value['desired'] else '暂停接单待确认'


def recent_error_text(values):
    return '；'.join(ERROR_LABELS[value] for value in values[-8:] if value in ERROR_LABELS) or '暂无已观察错误'
