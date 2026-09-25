"""保存本项目允许登记的领域资料类别。"""

ALLOWED_CATEGORIES = frozenset([
    "district_profile",
    "risk_profile",
    "officer_roster",
    "assistance_record"
])


def is_allowed_category(value: str) -> bool:
    """判断资料类别是否属于当前项目。"""

    return value in ALLOWED_CATEGORIES
