"""保存本项目允许登记的领域资料类别。"""

ALLOWED_CATEGORIES = frozenset([
    "district_profile",
    "risk_profile",
    "officer_roster",
    "assistance_record",
    "overdue_self_check",
    "open_hazard",
    "facility_anomaly",
    "urgent_event",
])

# 会产生风险加权的事实类别；帮扶与风险等级属于调节性资料，不直接计入风险分。
RISK_FACT_CATEGORIES = frozenset([
    "overdue_self_check",
    "open_hazard",
    "facility_anomaly",
    "urgent_event",
])


def is_allowed_category(value: str) -> bool:
    """判断资料类别是否属于当前项目。"""

    return value in ALLOWED_CATEGORIES
