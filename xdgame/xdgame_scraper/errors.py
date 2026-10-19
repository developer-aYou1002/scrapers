class LoginStateExpired(RuntimeError):
    """登录状态已失效，当前任务必须停止并提示重新登录。"""


class TaskInterrupted(RuntimeError):
    """用户主动中断，已完成的数据仍按既有事务边界保留。"""
