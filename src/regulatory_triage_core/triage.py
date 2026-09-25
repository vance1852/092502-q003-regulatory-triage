"""把风险资料编排成确定检查队列的纯规则逻辑。

本模块不接触数据库或当前时间，全部输入由服务层显式提供，便于对同一份事实
快照得到可复现的结果。编排结论分为四类：

- ``onsite``：当日现场检查；
- ``remote``：当日远程复核；
- ``assist_online``：仅安排线上帮扶（处于免访窗口、无紧急事件）；
- ``deferred``：因人力或片区时段容量不足被延后。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .audit import digest

# 现场与远程两类队列。
ONSITE = "onsite"
REMOTE = "remote"
ASSIST_ONLINE = "assist_online"
DEFERRED = "deferred"

# 紧急事件类别，可突破“无事不扰”的免访窗口。
URGENT = "urgent_event"

# 各风险事实类别的基础权重；紧急事件不参与普通加权，走单独的突破逻辑。
DEFAULT_FACTOR_WEIGHTS = {
    "overdue_self_check": 30,
    "open_hazard": 25,
    "facility_anomaly": 20,
}

# 企业风险等级对加权总分的乘子。
DEFAULT_RISK_MULTIPLIERS = {
    "high": 2.0,
    "medium": 1.2,
    "low": 1.0,
}

# 远程队列默认容量（未显式给出时）。
DEFAULT_REMOTE_CAPACITY = 8

# 帮扶后的默认免访窗口天数（“无事不扰”）。
DEFAULT_NO_VISIT_DAYS = 15

# 结论的稳定展示顺序。
DECISION_ORDER = (ONSITE, REMOTE, ASSIST_ONLINE, DEFERRED)


@dataclass(frozen=True)
class Fact:
    """单个、按业务键去重后的风险事实。"""

    fact_type: str
    external_key: str
    site_id: str
    payload: dict[str, Any]
    created_at: str
    payload_hash: str

    @property
    def key(self) -> str:
        return f"{self.fact_type}:{self.external_key}"


@dataclass(frozen=True)
class CandidateInput:
    """生成一家企业结论所需的全部资料。"""

    site_id: str
    district_id: str
    facts: tuple[Fact, ...]
    risk_level: str
    last_assistance_at: str | None
    exempt_until: str | None
    no_visit_days: int


@dataclass(frozen=True)
class Rules:
    """某个规则版本下的全部可调参数。"""

    version: int
    factor_weights: dict[str, int]
    risk_multipliers: dict[str, float]
    no_visit_days: int
    remote_capacity: int

    def content_hash(self) -> str:
        return digest({
            "factor_weights": self.factor_weights,
            "risk_multipliers": self.risk_multipliers,
            "no_visit_days": self.no_visit_days,
            "remote_capacity": self.remote_capacity,
        })


@dataclass
class ScoredSite:
    """打分后的企业及其中间结果，供解释接口复用。"""

    site_id: str
    district_id: str
    risk_level: str
    score: float
    urgent: bool
    exempt: bool
    window_broken: bool
    fact_keys: list[str] = field(default_factory=list)
    fact_contributions: dict[str, int] = field(default_factory=dict)
    risk_multiplier: float = 1.0
    reasons: list[str] = field(default_factory=list)
    decision: str = DEFERRED
    assigned_slot: str | None = None
    rank: int = 0


def latest_risk_level(records: list[dict[str, Any]]) -> str:
    """从风险档案中取最新一条的等级，缺省为 low。"""

    if not records:
        return "low"
    latest = max(records, key=lambda item: (item.get("created_at", ""), item.get("external_key", "")))
    level = str(latest.get("payload", {}).get("risk_level", "low")).lower()
    return level if level in ("high", "medium", "low") else "low"


def latest_assistance(records: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """返回最近一次帮扶的时间与免访截止时间（ISO 字符串可直接比较）。"""

    if not records:
        return None, None
    latest = max(records, key=lambda item: (item.get("created_at", ""), item.get("external_key", "")))
    occurred = latest.get("payload", {}).get("occurred_at") or latest.get("created_at")
    exempt_until = latest.get("payload", {}).get("exempt_until")
    return (occurred, exempt_until)


def build_facts(records_by_site: dict[str, list[dict[str, Any]]], fact_types: set[str]) -> dict[str, list[Fact]]:
    """把原始资料按 (类型, 业务键) 去重，同键只保留最新一条。

    这样同一条逾期/隐患/异常无论被同步多少次，都只会产生一个事实、计一次权重。
    """

    result: dict[str, list[Fact]] = {}
    for site_id, records in records_by_site.items():
        chosen: dict[tuple[str, str], dict[str, Any]] = {}
        for record in records:
            fact_type = record.get("category")
            if fact_type not in fact_types:
                continue
            key = (fact_type, record.get("external_key", ""))
            current = chosen.get(key)
            stamp = (record.get("created_at", ""), record.get("external_key", ""))
            if current is None or stamp > (current.get("created_at", ""), current.get("external_key", "")):
                chosen[key] = record
        facts = [
            Fact(fact_type=record["category"], external_key=record["external_key"], site_id=site_id,
                 payload=record.get("payload", {}), created_at=record.get("created_at", ""),
                 payload_hash=record.get("payload_hash", digest(record.get("payload", {}))))
            for record in chosen.values()
        ]
        facts.sort(key=lambda fact: (fact.fact_type, fact.external_key))
        result[site_id] = facts
    return result


def _shift_days(iso_value: str, days: int) -> str:
    """把一个 ISO 日期/日期时间字符串按整天平移后返回 ISO。"""

    from datetime import datetime, timedelta

    text = iso_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return iso_value
    return (parsed + timedelta(days=days)).isoformat()


def score_candidate(candidate: CandidateInput, rules: Rules, today: str) -> ScoredSite:
    """对一家企业打分并判断免访窗口是否被紧急事件突破。"""

    contributions: dict[str, int] = {}
    fact_keys: list[str] = []
    urgent = False
    urgent_basis: str | None = None
    base = 0
    for fact in candidate.facts:
        fact_keys.append(fact.key)
        if fact.fact_type == URGENT:
            urgent = True
            urgent_basis = fact.payload.get("trigger_basis") or fact.payload.get("basis") or fact.key
            continue
        weight = int(rules.factor_weights.get(fact.fact_type, 0))
        if weight:
            contributions[fact.key] = weight
            base += weight

    multiplier = float(rules.risk_multipliers.get(candidate.risk_level, 1.0))
    score = round(base * multiplier, 4)

    # 免访窗口：以最近帮扶时间加免访天数，或帮扶资料显式给出的截止时间为准。
    exempt_until = candidate.exempt_until
    if candidate.last_assistance_at and not exempt_until and candidate.no_visit_days > 0:
        exempt_until = _shift_days(candidate.last_assistance_at, candidate.no_visit_days)
    exempt = bool(exempt_until and today < exempt_until[:10])
    window_broken = exempt and urgent

    reasons: list[str] = []
    if base > 0:
        detail = "、".join(f"{key}({weight})" for key, weight in sorted(contributions.items()))
        reasons.append(f"风险事实加权 {base}（{detail}）×风险等级乘子 {multiplier:g}={score:g}")
    else:
        reasons.append("无风险事实，基础风险分为 0")
    if urgent:
        reasons.append(f"紧急事件凭触发依据突破免访窗口：{urgent_basis}")
    elif exempt:
        reasons.append(f"处于免访窗口（截止 {exempt_until}），按无事不扰安排线上帮扶")

    return ScoredSite(
        site_id=candidate.site_id, district_id=candidate.district_id, risk_level=candidate.risk_level,
        score=score, urgent=urgent, exempt=exempt, window_broken=window_broken,
        fact_keys=fact_keys, fact_contributions=contributions, risk_multiplier=multiplier, reasons=reasons,
    )


def allocate(candidates: list[CandidateInput], rules: Rules, today: str,
             onsite_capacity: int, district_capacity: dict[str, int],
             remote_capacity: int | None = None) -> list[ScoredSite]:
    """在人力与片区时段容量约束下确定性地分配队列。

    选择顺序固定为：紧急优先、再按风险分降序、最后按场所编号升序，因此同一份
    快照与容量在任何机器上结果一致。容量按“现场名额”和“片区当日现场名额”
    双重扣减，先到先占，超出者延后。
    """

    scored = [score_candidate(candidate, rules, today) for candidate in candidates]
    scored.sort(key=lambda item: (-int(item.urgent), -item.score, item.site_id))
    for rank, item in enumerate(scored, start=1):
        item.rank = rank

    remaining_onsite = max(0, onsite_capacity)
    remaining_district = {key: max(0, value) for key, value in district_capacity.items()}
    remaining_remote = max(0, remote_capacity if remote_capacity is not None else rules.remote_capacity)

    for item in scored:
        if item.urgent or item.window_broken or not item.exempt:
            # 需要接触企业：优先现场，容量不足时转远程复核，再不足则延后。
            district_left = remaining_district.get(item.district_id, 0)
            if remaining_onsite > 0 and district_left > 0:
                item.decision = ONSITE
                item.assigned_slot = f"onsite:{item.district_id}"
                remaining_onsite -= 1
                remaining_district[item.district_id] = district_left - 1
                item.reasons.append("占用当日现场名额与片区时段名额")
            elif remaining_remote > 0:
                item.decision = REMOTE
                item.assigned_slot = "remote"
                remaining_remote -= 1
                item.reasons.append("现场名额不足，降级为远程复核")
            else:
                item.decision = DEFERRED
                item.reasons.append("现场与远程名额均已满，列入延后")
        elif item.exempt:
            # 无紧急事件且处于免访窗口：仅线上帮扶，不占用现场名额。
            item.decision = ASSIST_ONLINE
            item.assigned_slot = "assist_online"
        else:
            item.decision = DEFERRED
            item.reasons.append("无入选必要，未安排队列")

    # 输出按场所编号稳定排序，便于持久化与解释。
    scored.sort(key=lambda item: item.site_id)
    return scored
