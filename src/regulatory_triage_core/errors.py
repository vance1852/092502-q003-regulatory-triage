"""领域服务使用的业务异常。"""


class DomainError(Exception):
    """所有可预期业务异常的基类。"""

    code = "domain_error"
    status = 400


class ValidationError(DomainError):
    """输入字段不符合业务约束。"""

    code = "validation_error"


class NotFoundError(DomainError):
    """请求引用的业务对象不存在。"""

    code = "not_found"
    status = 404


class PermissionDenied(DomainError):
    """操作者没有执行当前动作的权限。"""

    code = "permission_denied"
    status = 403


class ConflictError(DomainError):
    """请求编号或业务唯一键与既有内容冲突。"""

    code = "conflict"
    status = 409


class PlanVersionConflict(ConflictError):
    """名额操作携带的方案版本与当前方案不一致。"""

    code = "plan_version_conflict"


class DispatchStateConflict(ConflictError):
    """名额当前状态不允许该操作（如重复领取）。"""

    code = "dispatch_state_conflict"


class RuleConflict(ConflictError):
    """规则编号或版本与既有内容冲突。"""

    code = "rule_conflict"
