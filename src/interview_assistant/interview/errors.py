"""面试领域错误类型；独立成模块，避免组合对象与主服务循环导入。"""


class InterviewError(Exception):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        self.code, self.message, self.status = code, message, status


def conflict() -> InterviewError:
    return InterviewError("revision_conflict", "场次已被修改，请重新读取后再操作", 409)
