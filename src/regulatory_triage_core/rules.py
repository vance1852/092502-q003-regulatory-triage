"""风险事实派生与确定性编排规则引擎。

本模块全部为纯函数：给定领域资料、规则配置、日期、容量，
必然得到相同的评分与队列，不读取时钟、数据库或网络。
规则权重与事实状态严格分离——同一条事实（fact_key）在一个
方案中只出现一次、只贡献一次权重。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .audit import canonical_json, digest

# 事实类型
FACT_OVERDUE_SELF_CHECK = "overdue_self_check"
FACT_OPEN_HAZARD = "open_hazard"
FACT_FACILITY_ALERT = "facility_alert"
# 上下文事实：参与版本化与解释，但权重为 0，不重复计为风险
FACT_RISK_LEVEL = "risk_level"
FACT_RECENT_ASSISTANCE = "recent_assistance"

WEIGHTED_TYPES = frozenset([FACT_OVERDUE_SELF_CHECK, FACT_OPEN_HAZARD, FACT_FACILITY_ALERT])
CONTEXT_TYPES = frozenset([FACT_RISK_LEVEL, FACT_RECENT_ASSISTANCE])

ACTIONS = ("onsite", "remote", "assistance", "deferred")

DEFAULT_RULES: dict[str, Any] = {
    "weights": {
        FACT_OVERDUE_SELF_CHECK: 30.0,
        FACT_OPEN_HAZARD: 40.0,
        FACT_FACILITY_ALERT: 25.0,
    },
    "severity_weights": {
        "critical": 30.0,
        "major": 18.0,
        "minor": 8.0,
        "info": 0.0,
    },
    "risk_multipliers": {
        "high": 1.5,
        "medium": 1.0,
        "low": 0.7,
        "unknown": 1.0,
    },
    "thresholds": {
        "onsite": 60.0,
        "remote": 25.0,
    },
    "assistance_mitigation": 0.2,
    "no_visit_days": 30,
    "high_risk_break_enabled": True,
    "break_severities": ["critical"],
    "default_onsite_manpower": 4,
    "default_remote_manpower": 6,
}

RISK_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3}


def normalize_rules(payload: dict[str, Any] | None) -> dict[str, Any]:
    """用默认值补全并校验规则配置，返回排序稳定的规则对象。"""

    merged: dict[str, Any] = {}
    if payload:
        if not isinstance(payload, dict):
            raise ValueError("规则必须是对象")
        for key in ("weights", "severity_weights", "risk_multipliers", "thresholds"):
            section = payload.get(key)
            if section is not None and not isinstance(section, dict):
                raise ValueError(f"规则段 {key} 必须是对象")
        for key, value in payload.items():
            merged[key] = value
    for key, value in DEFAULT_RULES.items():
        merged.setdefault(key, value)
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                merged[key].setdefault(sub_key, sub_value)

    for section in ("weights", "severity_weights"):
        for key, value in merged[section].items():
            merged[section][key] = _number(value, f"{section}.{key}")
    for key, value in merged["risk_multipliers"].items():
        merged["risk_multipliers"][key] = _number(value, f"risk_multipliers.{key}")
    for key, value in merged["thresholds"].items():
        merged["thresholds"][key] = _number(value, f"thresholds.{key}")
    merged["assistance_mitigation"] = _number(merged["assistance_mitigation"], "assistance_mitigation")
    if not 0 <= merged["assistance_mitigation"] < 1:
        raise ValueError("assistance_mitigation 必须位于 [0, 1)")
    merged["no_visit_days"] = int(merged["no_visit_days"])
    if merged["no_visit_days"] < 0:
        raise ValueError("no_visit_days 不能为负")
    if not isinstance(merged["high_risk_break_enabled"], bool):
        raise ValueError("high_risk_break_enabled 必须是布尔值")
    if not isinstance(merged["break_severities"], list) or not all(
        isinstance(item, str) for item in merged["break_severities"]
    ):
        raise ValueError("break_severities 必须是字符串数组")
    return merged


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数字")
    if value < 0:
        raise ValueError(f"{field} 不能为负")
    return float(value)


def parse_day(value: Any) -> date:
    """把 YYYY-MM-DD 或 ISO 时间文本解析为日期。"""

    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("日期不能为空")
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"日期格式无效: {text}") from exc


class Fact:
    """一条去重后的版本化风险事实。"""

    __slots__ = ("fact_key", "site_id", "fact_type", "status", "severity",
                 "weight", "source_record_id", "source_hash", "evidence",
                 "observed_at", "active", "counts")

    def __init__(self, *, fact_key: str, site_id: str, fact_type: str, status: str,
                 severity: str, source_record_id: str, source_hash: str,
                 evidence: dict[str, Any], observed_at: str, active: bool,
                 counts: bool, weight: float = 0.0) -> None:
        self.fact_key = fact_key
        self.site_id = site_id
        self.fact_type = fact_type
        self.status = status
        self.severity = severity
        self.weight = weight
        self.source_record_id = source_record_id
        self.source_hash = source_hash
        self.evidence = evidence
        self.observed_at = observed_at
        self.active = active
        self.counts = counts

    def material(self) -> dict[str, Any]:
        """规则无关的事实材料：事实版本与事实摘要只依赖它。"""

        return {
            "fact_key": self.fact_key,
            "site_id": self.site_id,
            "fact_type": self.fact_type,
            "status": self.status,
            "severity": self.severity,
            "observed_at": self.observed_at,
            "active": self.active,
            "source_record_id": self.source_record_id,
            "source_hash": self.source_hash,
        }


def _latest_payloads(records: list[Any]) -> dict[tuple[str, str], Any]:
    """按 (category, external_key) 取最新一条资料记录。

    记录由存储层按插入顺序（rowid）提供，同一键后到的状态更新覆盖旧值，
    不依赖可能相同的 created_at 时间戳。
    """

    latest: dict[tuple[str, str], Any] = {}
    for record in records:
        latest[(record.category, record.external_key)] = record
    return latest


def _business_key(category: str, external_key: str, data: dict[str, Any]) -> str:
    """返回风险对象的稳定业务标识。

    资料表按 external_key 只追加；同一隐患/自查期/告警的状态更新
    （如隐患闭环）以新 external_key 登记，但载荷携带相同业务标识，
    事实据此归并为同一条，避免把状态更新误算成第二条风险。
    """

    if category == "self_check_report":
        return str(data.get("period") or external_key)
    if category == "hazard_record":
        return str(data.get("hazard_id") or external_key)
    if category == "facility_alert":
        return str(data.get("alert_id") or external_key)
    return external_key


def _latest_by_business(latest: dict[tuple[str, str], Any], category: str) -> dict[str, Any]:
    """在一个资料类别内按稳定业务标识取最新状态记录（后到覆盖）。"""

    grouped: dict[str, Any] = {}
    for (record_category, _external_key), record in latest.items():
        if record_category == category:
            grouped[_business_key(category, _external_key, record.payload)] = record
    return grouped


def derive_site_facts(site_id: str, records: list[Any], rules: dict[str, Any],
                      as_of: date) -> tuple[list[Fact], dict[str, Any]]:
    """把一个场所的领域资料派生为去重事实与评分上下文。"""

    facts: list[Fact] = []
    latest = _latest_payloads(records)

    risk_level = "unknown"
    risk_record = None
    assistance: list[tuple[date, Any]] = []

    for record in latest.values():
        data = record.payload
        if record.category == "risk_profile":
            level = str(data.get("level", "unknown")).lower()
            if level not in RISK_ORDER:
                level = "unknown"
            # 资料按到达顺序覆盖：以最后一条风险档案为准
            risk_level = level
            risk_record = record
        elif record.category == "assistance_record":
            try:
                assisted_on = parse_day(data.get("assisted_at", record.created_at))
            except ValueError:
                continue
            assistance.append((assisted_on, record))

    if risk_record is not None:
        facts.append(Fact(
            fact_key=f"{FACT_RISK_LEVEL}:{site_id}", site_id=site_id,
            fact_type=FACT_RISK_LEVEL, status=risk_level, severity="info",
            source_record_id=risk_record.record_id, source_hash=risk_record.payload_hash,
            evidence={"level": risk_level}, observed_at=risk_record.created_at,
            active=True, counts=False,
        ))

    latest_assistance = max(assistance, key=lambda item: item[0]) if assistance else None
    assistance_in_window = False
    window_allowed_after = None
    if latest_assistance is not None:
        assisted_on, assist_record = latest_assistance
        window_allowed_after = (assisted_on + timedelta(days=rules["no_visit_days"])).isoformat()
        assistance_in_window = as_of <= assisted_on + timedelta(days=rules["no_visit_days"])
        if assistance_in_window:
            facts.append(Fact(
                fact_key=f"{FACT_RECENT_ASSISTANCE}:{site_id}", site_id=site_id,
                fact_type=FACT_RECENT_ASSISTANCE, status="within_no_visit_window",
                severity="info", source_record_id=assist_record.record_id,
                source_hash=assist_record.payload_hash,
                evidence={"assisted_at": assisted_on.isoformat(),
                          "allowed_after": window_allowed_after},
                observed_at=assist_record.created_at, active=True, counts=False,
            ))

    for business_key, record in _latest_by_business(latest, "self_check_report").items():
        data = record.payload
        submitted = bool(data.get("submitted", False))
        due_day = parse_day(data.get("due_date")) if data.get("due_date") else None
        overdue = (not submitted) and due_day is not None and as_of > due_day
        if overdue:
            severity = str(data.get("severity", "major")).lower()
            facts.append(Fact(
                fact_key=f"{FACT_OVERDUE_SELF_CHECK}:{business_key}", site_id=site_id,
                fact_type=FACT_OVERDUE_SELF_CHECK, status="overdue", severity=severity,
                weight=rules["weights"][FACT_OVERDUE_SELF_CHECK]
                + rules["severity_weights"].get(severity, 0.0),
                source_record_id=record.record_id, source_hash=record.payload_hash,
                evidence={"period": data.get("period", business_key),
                          "due_date": due_day.isoformat()},
                observed_at=due_day.isoformat(), active=True, counts=True,
            ))
    for business_key, record in _latest_by_business(latest, "hazard_record").items():
        data = record.payload
        status = str(data.get("status", "open")).lower()
        if status not in ("closed", "resolved"):
            severity = str(data.get("severity", "major")).lower()
            facts.append(Fact(
                fact_key=f"{FACT_OPEN_HAZARD}:{business_key}", site_id=site_id,
                fact_type=FACT_OPEN_HAZARD, status=status, severity=severity,
                weight=rules["weights"][FACT_OPEN_HAZARD]
                + rules["severity_weights"].get(severity, 0.0),
                source_record_id=record.record_id, source_hash=record.payload_hash,
                evidence={"hazard_id": data.get("hazard_id", business_key),
                          "title": data.get("title", "")},
                observed_at=str(data.get("opened_at", record.created_at))[:10],
                active=True, counts=True,
            ))
    for business_key, record in _latest_by_business(latest, "facility_alert").items():
        data = record.payload
        status = str(data.get("status", "active")).lower()
        if status not in ("resolved", "cleared"):
            severity = str(data.get("severity", "minor")).lower()
            facts.append(Fact(
                fact_key=f"{FACT_FACILITY_ALERT}:{business_key}", site_id=site_id,
                fact_type=FACT_FACILITY_ALERT, status=status, severity=severity,
                weight=rules["weights"][FACT_FACILITY_ALERT]
                + rules["severity_weights"].get(severity, 0.0),
                source_record_id=record.record_id, source_hash=record.payload_hash,
                evidence={"facility": data.get("facility", ""),
                          "alert_id": data.get("alert_id", business_key)},
                observed_at=str(data.get("detected_at", record.created_at))[:10],
                active=True, counts=True,
            ))

    context = {
        "risk_level": risk_level,
        "assistance_in_window": assistance_in_window,
        "window_allowed_after": window_allowed_after,
    }
    return facts, context


def score_site(facts: list[Fact], context: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """对单个场所评分。每条计权事实只出现一次，倍率与帮扶缓解各只应用一次。"""

    contributions: list[dict[str, Any]] = []
    weighted_sum = 0.0
    severities: set[str] = set()
    severest_day = "9999-99-99"
    seen: set[str] = set()
    for fact in sorted(facts, key=lambda item: item.fact_key):
        if not fact.counts:
            continue
        if fact.fact_key in seen:  # 防御性去重：同一事实不得重复加权
            continue
        seen.add(fact.fact_key)
        weighted_sum += fact.weight
        severities.add(fact.severity)
        contributions.append({"fact_key": fact.fact_key, "fact_type": fact.fact_type,
                              "severity": fact.severity, "weight": round(fact.weight, 2)})
        day = fact.observed_at or "9999-99-99"
        if day < severest_day:
            severest_day = day

    risk_level = context["risk_level"]
    multiplier = rules["risk_multipliers"].get(risk_level, 1.0)
    unmitigated_score = round(weighted_sum * multiplier, 2)
    score = unmitigated_score
    mitigation_applied = False
    if context["assistance_in_window"] and weighted_sum > 0 and rules["assistance_mitigation"]:
        score *= 1 - rules["assistance_mitigation"]
        mitigation_applied = True
    score = round(score, 2)

    break_severity = bool(severities & set(rules["break_severities"]))
    high_risk_break = bool(
        rules["high_risk_break_enabled"]
        and weighted_sum > 0
        and (risk_level == "high" or break_severity)
    )
    return {
        "score": score,
        "unmitigated_score": unmitigated_score,
        "weighted_sum": round(weighted_sum, 2),
        "risk_multiplier": multiplier,
        "assistance_mitigation_applied": mitigation_applied,
        "contributions": contributions,
        "high_risk_break": high_risk_break,
        "earliest_observed_at": severest_day if weighted_sum > 0 else None,
    }


def _rank_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    """队列排序：分数降序、风险等级、最早事实日期、场所编号，全部确定。"""

    return (
        -candidate["scored"]["score"],
        RISK_ORDER.get(candidate["risk_level"], 3),
        candidate["scored"]["earliest_observed_at"] or "9999-99-99",
        candidate["site_id"],
    )


def normalize_slots(slots: list[dict[str, Any]] | None, rules: dict[str, Any]) -> list[dict[str, Any]]:
    """校验并排序时段容量；未给时段时用每日可用人力生成一个全天时段。"""

    if not slots:
        return [{"slot_id": "day", "start": "00:00", "end": "23:59",
                 "onsite_capacity": int(rules["default_onsite_manpower"]),
                 "remote_capacity": int(rules["default_remote_manpower"]),
                 "onsite_left": int(rules["default_onsite_manpower"]),
                 "remote_left": int(rules["default_remote_manpower"])}]
    normalized = []
    for slot in slots:
        slot_id = str(slot.get("slot_id", "")).strip()
        if not slot_id:
            raise ValueError("时段缺少 slot_id")
        start = str(slot.get("start", "")).strip()
        end = str(slot.get("end", "")).strip()
        try:
            datetime.strptime(start, "%H:%M")
            datetime.strptime(end, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"时段 {slot_id} 的 start/end 必须为 HH:MM") from exc
        onsite_capacity = int(slot.get("onsite_capacity", 0))
        remote_capacity = int(slot.get("remote_capacity", 0))
        if onsite_capacity < 0 or remote_capacity < 0:
            raise ValueError(f"时段 {slot_id} 容量不能为负")
        normalized.append({"slot_id": slot_id, "start": start, "end": end,
                           "onsite_capacity": onsite_capacity,
                           "remote_capacity": remote_capacity,
                           "onsite_left": onsite_capacity,
                           "remote_left": remote_capacity})
    return sorted(normalized, key=lambda item: (item["start"], item["slot_id"]))


def build_plan_items(*, sites: list[dict[str, Any]], slots: list[dict[str, Any]],
                     rules: dict[str, Any], thresholds: dict[str, float] | None = None,
                     emergencies: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """生成确定性的现场检查/远程复核/线上帮扶/延后队列。

    sites 元素：{site_id, facts, context, scored}。
    emergencies 元素：{emergency_id, site_id, trigger_type, trigger_reference, detail, declared_at}。
    """

    thresholds = thresholds or rules["thresholds"]
    emergency_by_site: dict[str, list[dict[str, Any]]] = {}
    for emergency in sorted(emergencies or [], key=lambda item: (item["declared_at"], item["emergency_id"])):
        emergency_by_site.setdefault(emergency["site_id"], []).append(emergency)

    candidates = sorted(
        sites,
        key=lambda candidate: (
            0 if emergency_by_site.get(candidate["site_id"]) else 1,
            *_rank_key(candidate),
        ),
    )
    items: list[dict[str, Any]] = []
    for candidate in candidates:
        site_id = candidate["site_id"]
        scored = candidate["scored"]
        context = candidate["context"]
        score = scored["score"]
        site_emergencies = emergency_by_site.get(site_id, [])
        emergency = site_emergencies[0] if site_emergencies else None

        within_window = bool(context["assistance_in_window"])
        override = False
        override_reason: str | None = None
        if emergency is not None:
            override = True
            override_reason = "emergency_trigger"
        elif within_window and scored["high_risk_break"]:
            override = True
            override_reason = "high_risk_break"

        # 高风险穿透成立时，现场资格按帮扶缓解前的分数判定，
        # 避免"连续帮扶/打卡"把高风险异常压到现场阈值之下。
        eligibility_score = (
            scored["unmitigated_score"]
            if scored["high_risk_break"] and scored["assistance_mitigation_applied"]
            else score
        )
        wants_onsite = emergency is not None or eligibility_score >= thresholds["onsite"]
        onsite_allowed = not within_window or override
        wants_remote = score >= thresholds["remote"]

        decision_reasons: list[str] = []
        if emergency is not None:
            decision_reasons.append(
                f"紧急事件 {emergency['trigger_reference']} 穿透免访窗口，强制现场检查"
            )
        elif wants_onsite:
            decision_reasons.append(
                f"评分 {eligibility_score} 达到现场阈值 {thresholds['onsite']}"
                + (f"（缓解前，缓解后 {score}）" if eligibility_score != score else "")
            )
        if within_window:
            decision_reasons.append(
                f"处于免访窗口（允许到访日 {context['window_allowed_after']}）"
            )
            if override and emergency is None:
                decision_reasons.append("高风险异常满足穿透条件，突破免访窗口")
        if scored["assistance_mitigation_applied"]:
            decision_reasons.append(
                f"近期帮扶缓解已应用一次（×{1 - rules['assistance_mitigation']:.2f}）"
            )

        action = "deferred"
        slot_id: str | None = None
        deferred_reason: str | None = None
        fallback_note: str | None = None

        if wants_onsite and onsite_allowed:
            slot_id = _take_capacity(slots, "onsite_left")
            if slot_id is not None:
                action = "onsite"
            else:
                slot = _take_capacity(slots, "remote_left")
                if slot is not None and (wants_remote or emergency is not None):
                    action = "remote"
                    slot_id = slot
                    fallback_note = "现场容量已满，紧急改安排远程复核" if emergency is not None \
                        else "现场容量已满，改安排远程复核"
                else:
                    deferred_reason = "onsite_capacity_exhausted"
        elif wants_onsite and within_window and not override:
            slot = _take_capacity(slots, "remote_left") if wants_remote else None
            if slot is not None:
                action = "remote"
                slot_id = slot
                decision_reasons.append("免访窗口内不进场，安排远程复核")
            elif wants_remote:
                deferred_reason = "remote_capacity_exhausted"
            else:
                action = "assistance"
                decision_reasons.append("免访窗口内仅安排线上帮扶")
        elif wants_remote:
            slot_id = _take_capacity(slots, "remote_left")
            if slot_id is not None:
                action = "remote"
            else:
                deferred_reason = "remote_capacity_exhausted"
        elif scored["weighted_sum"] > 0:
            action = "assistance"
            decision_reasons.append(
                f"评分 {score} 未达远程阈值 {thresholds['remote']}，仅安排线上帮扶"
            )
        else:
            deferred_reason = "no_active_risk_fact"

        if fallback_note:
            decision_reasons.append(fallback_note)
        if action == "deferred" and deferred_reason:
            decision_reasons.append(_deferred_text(deferred_reason))

        items.append({
            "site_id": site_id,
            "rank": 0,
            "action": action,
            "score": score,
            "risk_level": candidate["risk_level"],
            "slot_id": slot_id,
            "window_override": override,
            "deferred_reason": deferred_reason,
            "reasons": decision_reasons,
            "emergency": None if emergency is None else {
                "emergency_id": emergency["emergency_id"],
                "trigger_type": emergency["trigger_type"],
                "trigger_reference": emergency["trigger_reference"],
                "detail": emergency["detail"],
            },
            "scored": scored,
            "window": {
                "within_no_visit": within_window,
                "allowed_after": context["window_allowed_after"],
                "override": override,
                "override_reason": override_reason,
            },
        })
    for index, item in enumerate(items, start=1):
        item["rank"] = index
    return items


def _take_capacity(slots: list[dict[str, Any]], field: str) -> str | None:
    """从最早尚有容量的时段扣减一个名额，确定性分配。"""

    for slot in slots:
        if slot[field] > 0:
            slot[field] -= 1
            return slot["slot_id"]
    return None


def _deferred_text(reason: str) -> str:
    return {
        "onsite_capacity_exhausted": "当日现场名额已满，延后安排",
        "remote_capacity_exhausted": "当日远程复核名额已满，延后安排",
        "no_active_risk_fact": "无在期风险事实，无事不扰",
    }.get(reason, reason)


def facts_hash(facts: list[Fact]) -> str:
    """对规则无关的事实材料计算摘要（含状态，闭环后摘要改变）。"""

    material = [fact.material() for fact in sorted(facts, key=lambda item: item.fact_key)]
    return digest(material)


def rules_hash(rules: dict[str, Any]) -> str:
    return digest(canonical_json(rules))
