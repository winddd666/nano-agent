"""运行时控制信号；与用户退出和普通 API 故障分别处理。"""


class RequestCancelled(RuntimeError):
    """新消息要求立即处理，本次模型响应不能进入完整历史。"""


class RunTimedOut(RuntimeError):
    """The configured task wall clock budget has expired."""


def check_control(control):
    if control is not None and control():
        raise RequestCancelled("立即处理新输入，已取消本次模型请求。")
