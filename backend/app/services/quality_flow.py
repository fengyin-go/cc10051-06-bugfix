"""质量链路核心领域逻辑：检查运行账目、迟到回执、同轮匹配与幂等确认。

本模块不依赖路由层，quality_rule / quality_result 两个领域服务以及
专用 quality_flow 路由都从这里取同一套判定，避免“页面一套、接口一套”。

关键不变量：
1. 空检查（没有任何实际检查数据）一律保留空态与原因，绝不按满分/合格处理；
   只有确实执行了检查（检查总数 > 0，或批次明确产出数据）才参与评分。
2. 检查运行/回执只能汇入「同一实体编码 + 同一轮次」的报告，其他轮次哪怕已结束
   也不能补到当前空报告。
3. 同一份迟到回执重复送达只产生一次有效确认（账目 deliveries 可累加，
   confirmations 与看板指标只生成一次）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.store import store

RUN_TABLE = "quality_check_run"
CONFIRM_TABLE = "quality_confirmation"
CONTROL_TABLE = "quality_control"

DEFAULT_ROUND = "R1"

# 服务端判定的报告可用状态（code 稳定，label 直接给页面/下载件使用）。
STATE_EMPTY = "empty_waiting_receipt"
STATE_RUN_FOUND = "run_found_can_continue"
STATE_PUSH_INTERRUPTED = "push_interrupted"
STATE_READY = "ready"
STATE_HISTORICAL = "historical_scored"
STATE_FAILED = "generate_failed"

AVAILABILITY_LABELS: dict[str, str] = {
    STATE_EMPTY: "空态·待规则回执（暂无实际检查，不计合格）",
    STATE_RUN_FOUND: "已取得同轮规则运行号，可续办汇入并下载报告",
    STATE_PUSH_INTERRUPTED: "规则库推送中断·可凭规则运行号续办取得报告",
    STATE_READY: "已汇入实际检查，报告可下载",
    STATE_HISTORICAL: "历史有效评分，可继续回查",
    STATE_FAILED: "生成失败",
}

EMPTY_REASON = (
    "本报告检查批次没有任何实际检查数据（检查总数为 0），"
    "空结果不等于全部通过，暂不评分、不计合格；"
    "请等待质量规则库的同轮回执，或凭规则运行号办理续办。"
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm_round(value: Any = None) -> str:
    text = str(value if value is not None else "").strip()
    return text or DEFAULT_ROUND


def parse_int(value: Any, default: int = 0) -> int:
    """宽容地把表单/JSON 里的数字或数字字符串转成 int。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def is_numeric_count(value: Any) -> bool:
    """该字段是否是“真正的计数值”（0 也算），用于区分示例占位文本。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return bool(re.fullmatch(r"\s*-?\d+(?:\.0+)?\s*", value))
    return False


def score_pair(total: int, hits: int) -> tuple[float, str]:
    """返回 (0~1 的分数, 90.00% 形式的展示文本)。total 必须 > 0。"""
    safe_hits = max(0, min(hits, total))
    value = (total - safe_hits) / total
    return value, f"{value * 100:.2f}%"


def score_percent_number(score_text: Any) -> float | None:
    raw = str(score_text or "").strip().rstrip("%")
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((parse_int(r.get("id"), 0) for r in rows), default=0) + 1


def control() -> dict[str, Any]:
    """全局推送/回执控制开关（内存态，测试可随时中断/恢复）。"""
    rows = store.rows(CONTROL_TABLE)
    if not rows:
        rows.append({
            "id": 1,
            "push_interrupted": False,
            "status": "推送正常",
            "updated_at": now_iso(),
        })
    return rows[0]


def set_push_interrupted(interrupted: bool) -> dict[str, Any]:
    cfg = control()
    cfg["push_interrupted"] = bool(interrupted)
    cfg["status"] = "推送中断" if interrupted else "推送正常"
    cfg["updated_at"] = now_iso()
    return cfg


def push_is_interrupted() -> bool:
    return bool(control().get("push_interrupted"))


# ---------------------------------------------------------------------------
# 字段别名：兼容中文/英文、表单/JSON 的各种命名
# ---------------------------------------------------------------------------

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "entity": ("实体编码", "entity", "entity_code", "entityCode", "entity_id"),
    "round": ("轮次", "轮次号", "round", "round_no", "roundNo", "batch_round", "检查轮次"),
    "run_no": ("规则运行号", "检查运行号", "运行号", "run_no", "rule_run_no",
               "check_run_no", "runNo", "ruleRunNo"),
    "receipt_no": ("回执编号", "回执号", "receipt_no", "receiptNo", "receipt_id", "receiptId"),
    "total": ("检查总数", "检查批次总数", "批次总数", "检查数", "total", "checked", "total_count", "check_count", "totalCount"),
    "hits": ("命中数", "命中", "hits", "hit_count", "hitCount", "hit"),
    "rule_id": ("规则id", "规则ID", "rule_id", "ruleId", "quality_rule_id"),
    "rule_name": ("规则名称", "rule_name", "ruleName"),
    "report_id": ("报告id", "报告ID", "report_id", "reportId", "entry_id"),
    "report_no": ("报告编号", "report_no", "reportNo"),
    "batch_no": ("检查批次号", "批次号", "batch_no", "batchNo", "检查批次"),
    "interrupt": ("interrupt", "interrupted", "push_interrupted", "中断推送", "中断"),
    "reason": ("reason", "原因", "空态原因", "fix_hint", "remark", "说明"),
    "produced_data": ("批次产出数据", "产出数据", "produced_data", "has_data", "hasData"),
    "source": ("source", "来源", "渠道"),
}


def pick(payload: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    if not payload:
        return default
    for name in _FIELD_ALIASES.get(key, (key,)):  # type: ignore[arg-type]
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    return default


# ---------------------------------------------------------------------------
# 报告编号与报告投影
# ---------------------------------------------------------------------------

def report_no_of(report: dict[str, Any]) -> str:
    no = str(report.get("报告编号") or "").strip()
    if no:
        return no
    batch = str(report.get("报告批次") or "").strip()
    if batch:
        # 示例数据的批次是可读占位，也直接当作报告编号展示。
        report["报告编号"] = batch
        return batch
    no = f"QRPT-{parse_int(report.get('id')):04d}"
    report["报告编号"] = no
    return no


def report_round(report: dict[str, Any]) -> str:
    return norm_round(report.get("轮次"))


def report_entity(report: dict[str, Any]) -> str:
    return str(report.get("实体编码") or "").strip()


def report_has_real_score(report: dict[str, Any]) -> bool:
    """已经有有效检查支撑的历史评分，必须能继续回查，不能被空态逻辑覆盖。"""
    total = parse_int(report.get("检查总数"))
    score_num = score_percent_number(report.get("评分结果"))
    return (
        total > 0
        and score_num is not None
        and 0 <= score_num <= 100
        and str(report.get("status")) == "已生成"
    )


def present_report(report: dict[str, Any]) -> dict[str, Any]:
    """查询/动作返回前的统一投影：判定状态 + 别名字段，尽量兼容不同命名约定。"""
    projection = inspect_report(report)
    total = parse_int(report.get("检查总数"))
    hits = parse_int(report.get("命中数"))
    report["total"] = total
    report["hits"] = hits
    report["batch_total"] = total
    report["检查批次总数"] = str(total)
    report["确认次数"] = parse_int(report.get("有效确认次数"), 0)
    report["报告可用状态"] = str(report.get("可用状态", ""))
    report["availability"] = projection.get("state")
    return report


def list_runs(
    entity: str | None = None,
    round_value: Any = None,
    run_no: str | None = None,
) -> list[dict[str, Any]]:
    rows = list(store.rows(RUN_TABLE))
    if entity:
        rows = [r for r in rows if str(r.get("实体编码", "")) == str(entity).strip()]
    if round_value not in (None, ""):
        target = norm_round(round_value)
        rows = [r for r in rows if norm_round(r.get("轮次")) == target]
    if run_no:
        wanted = str(run_no).strip()
        rows = [r for r in rows if str(r.get("规则运行号", "")) == wanted]
    rows.sort(key=lambda r: parse_int(r.get("id")))
    return rows


def find_run(run_no: str) -> dict[str, Any] | None:
    wanted = str(run_no).strip()
    for run in store.rows(RUN_TABLE):
        if str(run.get("规则运行号", "")) == wanted:
            return run
    return None


def candidate_runs_for_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """同一实体 + 同一轮次的检查运行账目（其他轮次一律排除）。

    已确认给“别的报告”的运行也排除：一次有效确认只归属原报告，
    不能被另一条新建空报告继承，避免跨报告补结果。
    """
    report_id = parse_int(report.get("id"), 0)
    result: list[dict[str, Any]] = []
    for run in list_runs(entity=report_entity(report), round_value=report_round(report)):
        if run.get("confirmed"):
            owner = parse_int(run.get("confirmed_report_id"), 0)
            if owner and owner != report_id:
                continue
        if (
            parse_int(run.get("检查总数")) > 0
            or bool(run.get("批次产出数据"))
            or run.get("status") in ("已完成", "已结束", "回执已登记", "已确认")
        ):
            result.append(run)
    return result


def inspect_report(report: dict[str, Any]) -> dict[str, Any]:
    """服务端判定报告可用状态，并把可展示的投影字段写回报告行。

    写回的都是状态/候选信息，不产生确认、不推送看板；真正的一次性确认由
    confirm_run / continue_report 完成。
    """
    report_no = report_no_of(report)
    entity = report_entity(report)
    round_value = report_round(report)
    total = parse_int(report.get("检查总数"))
    hits = parse_int(report.get("命中数"))

    # 基线种子里的“样例N”占位行不是真实计数，也未被新流程管理：
    # 原样保留其历史字段，避免读投影把示例文本改写成空态。
    raw_total = report.get("检查总数")
    if (
        not report.get("quality_managed")
        and raw_total not in (None, "")
        and not is_numeric_count(raw_total)
    ):
        return {
            "state": "legacy_sample", "available": True, "empty": False,
            "legacy": True, "report_no": report_no, "run_no": "",
            "candidate_run_nos": [],
        }

    bound_run_no = str(report.get("规则运行号") or "").strip()
    bound_run = find_run(bound_run_no) if bound_run_no else None
    current_report_id = parse_int(report.get("id"), 0)
    if (
        bound_run is not None
        and bound_run.get("confirmed")
        and parse_int(bound_run.get("confirmed_report_id"), 0) not in (0, current_report_id)
    ):
        # 自动绑定到了“已确认给其他报告”的运行：该依据不属于本报告，解除绑定。
        bound_run = None
        bound_run_no = ""
        report["规则运行号"] = ""
    candidates = candidate_runs_for_report(report)
    candidate_nos = [str(r.get("规则运行号")) for r in candidates]
    bound_confirmed = bool(bound_run is not None and bound_run.get("confirmed"))

    # 注意：仅仅绑定/发现同轮运行号不等于已经汇入实际检查，
    # 只有确认过、或报告自身带实际检查量/标记时才算有检查。
    has_checks = (
        bool(report.get("has_actual_checks"))
        or str(report.get("批次产出数据", "")) == "是"
        or total > 0
        or bound_confirmed
    )

    # 已有有效检查的历史评分：保留原评分，标记可回查。
    if report_has_real_score(report) and not report.get("empty_state"):
        state = STATE_HISTORICAL if not report.get("规则运行号") else STATE_READY
        report["可用状态"] = AVAILABILITY_LABELS[state]
        report["availability_code"] = state
        report["available"] = True
        report["downloadable"] = True
        report["empty"] = False
        report["qualified"] = True
        report["轮次"] = round_value
        report["候选规则运行号"] = candidate_nos
        if not report.get("计分理由"):
            report["计分理由"] = (
                f"历史有效评分：实体 {entity or '—'}、轮次 {round_value}，"
                f"实际检查 {total} 项、命中 {hits} 项，评分 {report.get('评分结果')}。"
            )
        return {
            "state": state, "available": True, "empty": False,
            "report_no": report_no, "run_no": bound_run_no,
            "candidate_run_nos": candidate_nos,
        }

    if report.get("status") == "生成失败":
        state = STATE_FAILED
        report["可用状态"] = AVAILABILITY_LABELS[state]
        report["availability_code"] = state
        report["available"] = False
        report["downloadable"] = True
        report["empty"] = False
        report["qualified"] = False
        return {"state": state, "available": False, "empty": False,
                "report_no": report_no, "run_no": bound_run_no,
                "candidate_run_nos": candidate_nos}

    # 已绑定且已确认：正式可下载。
    if bound_run is not None and bound_run.get("confirmed"):
        state = STATE_READY
        report["可用状态"] = AVAILABILITY_LABELS[state]
        report["availability_code"] = state
        report["available"] = True
        report["downloadable"] = True
        report["empty"] = False
        report["qualified"] = hits == 0
        report["有效确认次数"] = parse_int(bound_run.get("有效确认次数"), 1)
        return {"state": state, "available": True, "empty": False,
                "report_no": report_no, "run_no": bound_run_no,
                "candidate_run_nos": candidate_nos}

    # total > 0：检查确实执行过，哪怕命中 0 也照旧满分。
    if total > 0:
        value, text = score_pair(total, hits)
        report["评分结果"] = text
        report["score"] = round(value, 4)
        report["score_percent"] = round(value * 100, 2)
        report["empty_state"] = False
        report["available"] = True
        report["downloadable"] = True
        report["empty"] = False
        report["qualified"] = hits == 0
        report["可用状态"] = AVAILABILITY_LABELS[STATE_RUN_FOUND] if candidates and not bound_confirmed else AVAILABILITY_LABELS[STATE_READY]
        report["availability_code"] = STATE_RUN_FOUND if candidates and not bound_confirmed else STATE_READY
        report["轮次"] = round_value
        report["候选规则运行号"] = candidate_nos
        return {"state": report["availability_code"], "available": True,
                "empty": False, "report_no": report_no,
                "run_no": bound_run_no, "candidate_run_nos": candidate_nos}

    # 真正的空态：total == 0 且没有任何同轮实际检查。
    run_no = bound_run_no or (candidate_nos[0] if len(candidate_nos) == 1 else "")
    interrupted = push_is_interrupted() or any(
        r.get("推送状态") == "中断" for r in candidates
    )
    if run_no and interrupted:
        state = STATE_PUSH_INTERRUPTED
    elif run_no:
        state = STATE_RUN_FOUND
    else:
        state = STATE_EMPTY

    report["轮次"] = round_value
    report["empty_state"] = True
    report["empty"] = True
    report["available"] = False
    report["downloadable"] = True  # 空态件本身允许下载，内容与状态绑定
    report["qualified"] = False   # 空结果绝不能算合格
    report["score"] = None
    report["score_percent"] = None
    report["评分结果"] = "空态·暂无评分"
    report["空态原因"] = report.get("空态原因") or EMPTY_REASON
    report["可用状态"] = AVAILABILITY_LABELS[state]
    report["availability_code"] = state
    report["候选规则运行号"] = candidate_nos
    if run_no:
        report["规则运行号"] = run_no
    report["计分理由"] = (
        "暂无计分依据：本批次没有实际检查数据；"
        + (f"已发现同轮规则运行号 {run_no}，可凭号续办。" if run_no
           else "规则库尚无同轮检查回执，仍待规则回执。")
    )
    return {"state": state, "available": False, "empty": True,
            "report_no": report_no, "run_no": run_no,
            "candidate_run_nos": candidate_nos}


# ---------------------------------------------------------------------------
# 看板指标（只有真正汇入实际检查后才发送）
# ---------------------------------------------------------------------------

def push_quality_metric(report: dict[str, Any]) -> int:
    """把已确认报告的质量指标写入治理看板；每个报告只推一次。"""
    if not report.get("has_actual_checks") and parse_int(report.get("检查总数")) <= 0:
        return 0
    existing_id = report.get("stats_id")
    if existing_id:
        for row in store.rows("stats_overview"):
            if parse_int(row.get("id")) == parse_int(existing_id):
                return parse_int(existing_id)

    stats = store.rows("stats_overview")
    stat_id = _next_id(stats)
    score_text = str(report.get("评分结果", "0%"))
    score_num = score_percent_number(score_text)
    if score_num is None:
        score_num = 0.0
    abnormal = score_num < 90.0
    total = parse_int(report.get("检查总数"))
    hits = parse_int(report.get("命中数"))
    rationale = report.get("计分理由") or (
        f"同一实体 {report_entity(report)}、同一轮次 {report_round(report)}："
        f"实际检查 {total} 项，命中 {hits} 项，得分 {score_text}。"
    )
    stats.append({
        "id": stat_id,
        "统计时间": now_iso(),
        "实体编码": report_entity(report),
        "金记录数": str(total),
        "重复率": score_text,
        "分发延迟": "0",
        # 质量指标专用字段
        "指标类型": "质量评分",
        "质量评分": score_text,
        "检查总数": str(total),
        "命中数": str(hits),
        "轮次": report_round(report),
        "报告编号": report_no_of(report),
        "报告批次": report.get("报告批次", ""),
        "规则运行号": report.get("规则运行号", ""),
        "回执编号": report.get("回执编号", ""),
        "指标依据": rationale,
        "计分理由": rationale,
        "status": "指标越界" if abnormal else "正常",
        "pending": abnormal,
        "abnormal": abnormal,
    })
    report["stats_id"] = stat_id
    return stat_id


def quality_basis_overview() -> dict[str, Any]:
    """看板“当前依据”：已汇入的质量指标 + 空态报告的不计合格依据。"""
    metrics: list[dict[str, Any]] = []
    empty_basis: list[dict[str, Any]] = []
    for report in store.rows("quality_result"):
        projection = inspect_report(report)
        item = {
            "report_id": report.get("id"),
            "报告编号": report_no_of(report),
            "报告批次": report.get("报告批次", ""),
            "实体编码": report_entity(report),
            "轮次": report_round(report),
            "规则运行号": report.get("规则运行号", ""),
            "检查总数": parse_int(report.get("检查总数")),
            "命中数": parse_int(report.get("命中数")),
            "评分结果": report.get("评分结果", ""),
            "计分理由": report.get("计分理由", ""),
            "available": projection["available"],
            "empty": projection["empty"],
            "state": projection["state"],
            "stats_id": report.get("stats_id"),
        }
        if projection["empty"] or not projection["available"]:
            empty_basis.append(item)
        elif report.get("stats_id") or report.get("has_actual_checks"):
            metrics.append(item)
    stat_rows = [
        {
            "id": r.get("id"),
            "实体编码": r.get("实体编码", ""),
            "质量评分": r.get("质量评分", r.get("重复率", "")),
            "报告编号": r.get("报告编号", ""),
            "规则运行号": r.get("规则运行号", ""),
            "轮次": r.get("轮次", ""),
            "计分理由": r.get("计分理由", r.get("指标依据", "")),
            "status": r.get("status", ""),
        }
        for r in store.rows("stats_overview")
        if r.get("指标类型") == "质量评分" or r.get("计分理由")
    ]
    return {"metrics": metrics, "empty_basis": empty_basis,
            "stats": stat_rows, "push_interrupted": push_is_interrupted()}


# ---------------------------------------------------------------------------
# 幂等确认
# ---------------------------------------------------------------------------

def confirm_run(
    run: dict[str, Any],
    report: dict[str, Any],
    *,
    source: str = "push",
) -> dict[str, Any]:
    """把一次检查运行的真实结果确认汇入报告。同一 run 永远只确认一次。"""
    # 实体 + 轮次硬校验，防止跨实体/跨轮次补空报告。
    if report_entity(report) != str(run.get("实体编码", "")).strip():
        return {"ok": False, "confirmed": False, "created": False,
                "message": "检查记录与报告不是同一实体编码，不能汇入该报告"}
    if report_round(report) != norm_round(run.get("轮次")):
        return {"ok": False, "confirmed": False, "created": False,
                "message": "检查记录与报告不属于同一轮次，其他轮次已结束的结果不能补到空报告"}

    # 幂等：同一份迟到结果只确认一次。
    if run.get("confirmed"):
        confirmation_id = run.get("confirmation_id")
        existing = None
        for c in store.rows(CONFIRM_TABLE):
            if parse_int(c.get("id")) == parse_int(confirmation_id):
                existing = c
                break
        return {"ok": True, "confirmed": True, "created": False,
                "duplicate": True, "confirmation": existing,
                "stat_id": report.get("stats_id"),
                "message": "该检查结果已经有效确认，不重复确认"}

    # 报告级保护：同一报告若已由同轮的另一条运行有效确认，也不再产生第二次确认。
    if report.get("has_actual_checks") and parse_int(report.get("有效确认次数")) >= 1:
        return {"ok": True, "confirmed": True, "created": False,
                "duplicate": True, "stat_id": report.get("stats_id"),
                "message": "该报告已有同轮有效确认，不重复确认"}

    total = parse_int(run.get("检查总数"))
    hits = parse_int(run.get("命中数"))
    if total <= 0:
        return {"ok": False, "confirmed": False, "created": False,
                "message": "该运行没有实际检查项（检查总数为 0），不能确认出有效评分"}

    value, text = score_pair(total, hits)
    rationale = (
        f"同一实体 {report_entity(report)}、同一轮次 {report_round(report)}："
        f"凭规则运行号 {run.get('规则运行号')}（回执 {run.get('回执编号') or '—'}）"
        f"汇入实际检查 {total} 项，命中 {hits} 项，"
        f"得分 ({total}-{hits})/{total} = {text}。"
    )

    report["检查总数"] = str(total)
    report["命中数"] = str(hits)
    report["评分结果"] = text
    report["score"] = round(value, 4)
    report["score_percent"] = round(value * 100, 2)
    report["score_decimal"] = f"{value:.2f}"
    report["status"] = "已生成"
    report["pending"] = False
    report["abnormal"] = hits > 0
    report["empty_state"] = False
    report["empty"] = False
    report["available"] = True
    report["downloadable"] = True
    report["qualified"] = hits == 0
    report["has_actual_checks"] = True
    report["批次产出数据"] = "是" if run.get("批次产出数据", True) else "否"
    report["规则运行号"] = run.get("规则运行号", "")
    report["回执编号"] = run.get("回执编号", "")
    report["轮次"] = norm_round(run.get("轮次"))
    report["检查批次号"] = run.get("检查批次号", report.get("检查批次号", ""))
    report["计分理由"] = rationale
    report["可用状态"] = AVAILABILITY_LABELS[STATE_READY]
    report["availability_code"] = STATE_READY

    confirmations = store.rows(CONFIRM_TABLE)
    confirmation_id = _next_id(confirmations)
    confirmation = {
        "id": confirmation_id,
        "确认编号": f"QCFM-{confirmation_id:04d}",
        "规则运行号": run.get("规则运行号", ""),
        "回执编号": run.get("回执编号", ""),
        "report_id": report.get("id"),
        "报告编号": report_no_of(report),
        "实体编码": report_entity(report),
        "轮次": report_round(report),
        "检查总数": total,
        "命中数": hits,
        "评分结果": text,
        "source": source,
        "created_at": now_iso(),
    }
    confirmations.append(confirmation)

    run["confirmed"] = True
    run["status"] = "已确认"
    run["推送状态"] = "已汇入报告"
    run["有效确认次数"] = 1
    run["confirmation_id"] = confirmation_id
    run["confirmed_report_id"] = report.get("id")
    run["confirmed_at"] = confirmation["created_at"]

    report["有效确认次数"] = 1
    stat_id = push_quality_metric(report)
    return {"ok": True, "confirmed": True, "created": True,
            "duplicate": False, "confirmation": confirmation,
            "stat_id": stat_id, "rationale": rationale,
            "message": f"已确认同轮检查结果，报告评分 {text}"}


def find_target_report(run: dict[str, Any], report_id: Any = None) -> dict[str, Any] | None:
    """按显式 id 或同实体+同轮规则寻找待补的空报告。"""
    if report_id not in (None, ""):
        try:
            report = store.find("quality_result", int(report_id))
        except (TypeError, ValueError):
            report = None
        if report is not None:
            return report
    same = [
        r for r in store.rows("quality_result")
        if report_entity(r) == str(run.get("实体编码", "")).strip()
        and report_round(r) == norm_round(run.get("轮次"))
    ]
    # 优先尚未汇入实际检查的报告，取最新一条。
    waiting = [r for r in same if not r.get("has_actual_checks")
               and not report_has_real_score(r)]
    pool = waiting or same
    if not pool:
        return None
    return max(pool, key=lambda r: parse_int(r.get("id")))


def attempt_dispatch(run: dict[str, Any], *, report_id: Any = None) -> dict[str, Any]:
    """规则库 -> 报告的推送。中断时保留账目，不确认、不发看板指标。"""
    if push_is_interrupted():
        run["推送状态"] = "中断"
        return {"ok": False, "dispatched": False, "interrupted": True,
                "message": "规则库向报告推送中断，检查运行账目已独立保留，可凭规则运行号续办"}
    report = find_target_report(run, report_id)
    if report is None:
        run["推送状态"] = "待匹配报告"
        return {"ok": True, "dispatched": False, "interrupted": False,
                "message": "同轮暂无对应质量报告，回执已在规则库登记为同轮依据"}
    result = confirm_run(run, report, source="push")
    if result.get("ok"):
        run["推送状态"] = "已推送" if result.get("created") else run.get("推送状态", "已汇入报告")
    return result


# ---------------------------------------------------------------------------
# 迟到回执登记（幂等）
# ---------------------------------------------------------------------------

def _register_rule_basis(run: dict[str, Any], rule_id: Any = None) -> None:
    """质量规则库为收到的检查登记同轮依据。"""
    rules = store.rows("quality_rule")
    target: dict[str, Any] | None = None
    if rule_id not in (None, ""):
        target = store.find("quality_rule", parse_int(rule_id, -1))
    if target is None:
        same_entity = [r for r in rules
                       if str(r.get("实体编码", "")) == str(run.get("实体编码", "")).strip()]
        enabled = [r for r in same_entity if r.get("status") == "已启用"]
        target = enabled[0] if enabled else (same_entity[0] if same_entity else None)

    basis = (
        f"[{now_iso()}] 轮次 {norm_round(run.get('轮次'))} "
        f"规则运行号 {run.get('规则运行号')} / 回执 {run.get('回执编号') or '—'}："
        f"实际检查 {run.get('检查总数')} 项，命中 {run.get('命中数')} 项"
    )
    run["同轮依据"] = basis
    if target is not None:
        entries = target.setdefault("同轮依据登记", [])
        if isinstance(entries, list):
            entries.append({
                "轮次": norm_round(run.get("轮次")),
                "规则运行号": run.get("规则运行号"),
                "回执编号": run.get("回执编号", ""),
                "检查总数": run.get("检查总数"),
                "命中数": run.get("命中数"),
                "basis": basis,
                "at": now_iso(),
            })
        target["最新同轮依据"] = basis
        target["同轮"] = norm_round(run.get("轮次"))
        run["rule_id"] = target.get("id")


def register_receipt(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    entity = str(pick(payload, "entity", "")).strip()
    round_value = norm_round(pick(payload, "round"))
    receipt_no = str(pick(payload, "receipt_no", "") or "").strip()
    run_no = str(pick(payload, "run_no", "") or "").strip()
    total = parse_int(pick(payload, "total", 0))
    hits = parse_int(pick(payload, "hits", 0))
    batch_no = str(pick(payload, "batch_no", "") or "").strip()
    explicit_interrupt = pick(payload, "interrupt", None)
    report_id = pick(payload, "report_id", None)
    source = str(pick(payload, "source", "receipt") or "receipt")

    # 支持直接给检查明细数组：10 条检查、其中 1 条命中。
    checks = payload.get("checks") or payload.get("records") or payload.get("items")
    if isinstance(checks, list) and checks:
        if not total:
            total = len(checks)
        if not hits:
            hits = sum(
                1 for c in checks
                if isinstance(c, dict) and (
                    c.get("hit") in (True, "true", "True", "1", 1)
                    or str(c.get("是否命中", "")).strip() in ("是", "命中", "true", "1")
                )
            )

    produced_data = pick(payload, "produced_data", None)
    if produced_data is None:
        produced_data = total > 0 or bool(checks)
    else:
        produced_data = str(produced_data).lower() not in ("false", "0", "否")

    if hits > total and total > 0:
        return {"ok": False, "message": "命中数超过检查总数，数据异常，未登记"}

    # 幂等键：优先回执编号，其次规则运行号。
    # 只有在调用方“两个显式编号都没给”时，才用 实体+轮次+结果摘要 兜底去重；
    # 若调用方显式给了新的运行号/回执号却没查到，说明这是另一次运行，不能按摘要合并。
    digest = f"{entity}|{round_value}|{total}|{hits}"
    existing: dict[str, Any] | None = None
    if receipt_no:
        for r in store.rows(RUN_TABLE):
            if str(r.get("回执编号", "")) == receipt_no:
                existing = r
                break
    if existing is None and run_no:
        existing = find_run(run_no)
    if existing is None and not receipt_no and not run_no:
        for r in list_runs(entity=entity, round_value=round_value):
            if str(r.get("结果摘要", "")) == digest:
                existing = r
                break

    if explicit_interrupt is not None:
        want_interrupt = str(explicit_interrupt).lower() not in ("false", "0", "否", "none")
        if want_interrupt:
            set_push_interrupted(True)

    if existing is not None:
        existing["deliveries"] = parse_int(existing.get("deliveries"), 1) + 1
        if explicit_interrupt is not None and str(explicit_interrupt).lower() in ("false", "0", "否"):
            set_push_interrupted(False)
        dispatch = attempt_dispatch(existing, report_id=report_id)
        return {
            "ok": True, "duplicate": True, "idempotent": True,
            "run": _public_run(existing),
            "run_no": existing.get("规则运行号"),
            "confirmation_count": parse_int(existing.get("有效确认次数"), 0),
            "deliveries": existing["deliveries"],
            "dispatch": dispatch,
            "message": "同一份迟到回执重复送达，已按幂等处理，不重复确认",
        }

    runs = store.rows(RUN_TABLE)
    run_id = _next_id(runs)
    if not run_no:
        run_no = f"QRUN-{round_value}-{run_id:04d}"
    run = {
        "id": run_id,
        "规则运行号": run_no,
        "回执编号": receipt_no or f"QRC-{run_id:04d}",
        "实体编码": entity,
        "轮次": round_value,
        "检查总数": str(total),
        "命中数": str(hits),
        "结果摘要": digest,
        "检查批次号": batch_no or f"BATCH-{run_id:04d}",
        "批次产出数据": bool(produced_data),
        "status": "已完成",
        "回执状态": "已送达规则库",
        "推送状态": "待推送",
        "有效确认次数": 0,
        "confirmed": False,
        "deliveries": 1,
        "source": source,
        "created_at": now_iso(),
        "检查明细": checks if isinstance(checks, list) else [],
    }
    runs.append(run)
    _register_rule_basis(run, rule_id=pick(payload, "rule_id", None))

    dispatch = attempt_dispatch(run, report_id=report_id)
    return {
        "ok": True, "duplicate": False,
        "run": _public_run(run),
        "run_no": run_no,
        "confirmation_count": 0,
        "deliveries": 1,
        "dispatch": dispatch,
        "message": "迟到检查回执已在质量规则库登记同轮依据",
    }


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run.get("id"),
        "规则运行号": run.get("规则运行号"),
        "回执编号": run.get("回执编号"),
        "实体编码": run.get("实体编码"),
        "轮次": norm_round(run.get("轮次")),
        "检查总数": parse_int(run.get("检查总数")),
        "命中数": parse_int(run.get("命中数")),
        "status": run.get("status"),
        "回执状态": run.get("回执状态"),
        "推送状态": run.get("推送状态"),
        "有效确认次数": parse_int(run.get("有效确认次数"), 0),
        "confirmed": bool(run.get("confirmed")),
        "deliveries": parse_int(run.get("deliveries"), 1),
        "同轮依据": run.get("同轮依据", ""),
        "confirmed_report_id": run.get("confirmed_report_id"),
        "confirmation_id": run.get("confirmation_id"),
    }


# ---------------------------------------------------------------------------
# 报告续办
# ---------------------------------------------------------------------------

def continue_report(
    report_id: int,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """值班人员从报告下载/续办入口主动办理：可凭规则运行号拉取同轮结果。"""
    payload = payload or {}
    report = store.find("quality_result", report_id)
    if report is None:
        return {"ok": False, "message": f"质量报告 {report_id} 不存在"}

    inspect_report(report)
    run_no = str(pick(payload, "run_no", "") or report.get("规则运行号") or "").strip()
    run: dict[str, Any] | None = find_run(run_no) if run_no else None

    if run is None:
        candidates = candidate_runs_for_report(report)
        if len(candidates) == 1:
            run = candidates[0]
        elif candidates:
            return {
                "ok": False, "waiting": True,
                "candidate_run_nos": [r.get("规则运行号") for r in candidates],
                "message": "发现多个同轮规则运行号，请指定要凭哪一个规则运行号续办",
            }

    if run is None:
        report["可用状态"] = AVAILABILITY_LABELS[STATE_EMPTY]
        return {
            "ok": True, "waiting": True, "confirmed": False,
            "state": STATE_EMPTY,
            "report_no": report_no_of(report),
            "message": "仍待规则回执：规则库还没有同一实体、同一轮次的检查运行账目",
        }

    # 显式凭号时也要过实体+轮次校验（confirm_run 内统一拦截）。
    result = confirm_run(run, report, source=pick(payload, "source", "continue") or "continue")
    result["report"] = report
    result["report_no"] = report_no_of(report)
    result["run_no"] = run.get("规则运行号")
    return result


def retry_receipt(
    run_no: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """恢复回执/推送并重试一次。已确认的运行不会产生第二次有效确认。"""
    payload = payload or {}
    set_push_interrupted(False)
    target_no = run_no or str(pick(payload, "run_no", "") or "").strip()
    receipt_no = str(pick(payload, "receipt_no", "") or "").strip()
    run = find_run(target_no) if target_no else None
    if run is None and receipt_no:
        for r in store.rows(RUN_TABLE):
            if str(r.get("回执编号", "")) == receipt_no:
                run = r
                break
    if run is None:
        # 不指定具体运行时，重试所有中断中的运行。
        interrupted = [r for r in store.rows(RUN_TABLE) if r.get("推送状态") == "中断"]
        if not interrupted:
            return {"ok": True, "retried": 0,
                    "message": "推送已恢复，没有需要重试的中断回执"}
        results = [retry_receipt(None, {"run_no": r.get("规则运行号")}) for r in interrupted]
        return {"ok": all(bool(r.get("ok")) for r in results), "retried": len(results),
                "results": results}
    result = attempt_dispatch(run, report_id=pick(payload, "report_id", None))
    result["run_no"] = run.get("规则运行号")
    result["confirmation_count"] = parse_int(run.get("有效确认次数"), 0)
    return result


# ---------------------------------------------------------------------------
# 状态绑定的报告下载件
# ---------------------------------------------------------------------------

def render_report_download(report: dict[str, Any], *, fmt: str = "md") -> tuple[str, str, str]:
    """返回 (正文, 文件名, media_type)。内容严格绑定服务端判定状态。"""
    projection = inspect_report(report)
    report_no = report_no_of(report)
    run_no = str(report.get("规则运行号") or projection.get("run_no") or "")
    safe_no = str(report_no).replace("/", "-").replace(" ", "_")

    if projection["state"] == STATE_READY or report_has_real_score(report):
        title = f"质量报告（正式） {report_no}"
        verdict = "合格（零命中，全部通过）" if parse_int(report.get("命中数")) == 0 else "存在命中，按评分口径计分"
        body_lines = [
            f"# {title}",
            f"- 报告编号: {report_no}",
            f"- 报告批次: {report.get('报告批次', '')}",
            f"- 实体编码: {report_entity(report)}",
            f"- 轮次: {report_round(report)}",
            f"- 规则运行号: {run_no or '—'}",
            f"- 回执编号: {report.get('回执编号', '') or '—'}",
            f"- 检查批次号: {report.get('检查批次号', '') or '—'}",
            f"- 检查总数: {report.get('检查总数')}",
            f"- 命中数: {report.get('命中数')}",
            f"- 评分结果: {report.get('评分结果')}",
            f"- 可用性: {report.get('可用状态')}",
            f"- 结论: {verdict}",
            f"- 计分理由: {report.get('计分理由', '')}",
            f"- 有效确认次数: {report.get('有效确认次数', 1)}",
            "",
            "本报告已汇入同一实体、同一轮次的实际检查结果。",
        ]
        score_text = str(report.get("评分结果", ""))
        filename = f"quality-report-{safe_no}-{score_text.rstrip('%')}.{ 'csv' if fmt == 'csv' else 'md'}"
    else:
        candidate_nos = projection.get("candidate_run_nos") or []
        title = f"质量报告（空态件） {report_no}"
        body_lines = [
            f"# {title}",
            f"- 报告编号: {report_no}",
            f"- 报告批次: {report.get('报告批次', '')}",
            f"- 实体编码: {report_entity(report)}",
            f"- 轮次: {report_round(report)}",
            "- 检查总数: 0",
            "- 命中数: 0",
            "- 评分结果: 空态·暂无评分（不是 100%，也不是合格）",
            f"- 可用状态: {report.get('可用状态', AVAILABILITY_LABELS[STATE_EMPTY])}",
            f"- 空态原因: {report.get('空态原因') or EMPTY_REASON}",
            f"- 规则运行号: {run_no or '尚未取得'}",
            f"- 同轮候选运行号: {', '.join(candidate_nos) if candidate_nos else '无（仍待规则回执）'}",
            "",
            "办理提示: 在质量报告原行点击「续办」，或调用 "
            f"POST /api/quality_result/{report.get('id')}/continue 并附规则运行号；"
            "汇入同轮真实检查后即可下载含真实评分的正式报告。",
        ]
        filename = f"quality-report-{safe_no}-empty.{ 'csv' if fmt == 'csv' else 'md'}"

    if fmt == "csv":
        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["报告编号", "实体编码", "轮次", "规则运行号", "检查总数",
                         "命中数", "评分结果", "可用状态", "计分理由/空态原因"])
        writer.writerow([
            report_no, report_entity(report), report_round(report), run_no,
            report.get("检查总数", "0"), report.get("命中数", "0"),
            report.get("评分结果", ""), report.get("可用状态", ""),
            report.get("计分理由") or report.get("空态原因", ""),
        ])
        return "﻿" + buffer.getvalue(), filename, "text/csv; charset=utf-8"

    return "\n".join(body_lines) + "\n", filename, "text/markdown; charset=utf-8"


def download_with_run(report_id: int, run_no: str | None) -> dict[str, Any]:
    """下载入口允许凭规则运行号先续办，再返回与最新状态绑定的下载结果。"""
    report = store.find("quality_result", report_id)
    if report is None:
        return {"ok": False, "status_code": 404, "message": "报告不存在"}
    if run_no:
        result = continue_report(report_id, {"run_no": run_no, "source": "download"})
        if not result.get("ok"):
            return {"ok": False, "status_code": 409, "message": result.get("message", "续办失败")}
    else:
        inspect_report(report)
    content, filename, media_type = render_report_download(
        report, fmt="md"
    )
    return {"ok": True, "content": content, "filename": filename,
            "media_type": media_type, "report": report}


# ---------------------------------------------------------------------------
# 一键布置题面中的中断场景（供界面/演练使用，确定性测试通常自行造数）
# ---------------------------------------------------------------------------

def bootstrap_scenario(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    entity = str(pick(payload, "entity", "QUAL-S11-001") or "QUAL-S11-001")
    round_value = norm_round(pick(payload, "round", "R20261126"))
    interrupt = payload.get("interrupt", True)
    with_receipt = payload.get("with_receipt", True)

    results = store.rows("quality_result")
    existing_report = next(
        (r for r in results if report_entity(r) == entity and report_round(r) == round_value),
        None,
    )
    if existing_report is None:
        report_id = _next_id(results)
        report = {
            "id": report_id,
            "报告批次": f"QR-BATCH-{report_id:04d}",
            "报告编号": f"QRPT-{report_id:04d}",
            "实体编码": entity,
            "轮次": round_value,
            "检查总数": "0",
            "命中数": "0",
            "评分结果": "空态·暂无评分",
            "status": "生成中",
            "pending": True,
            "abnormal": False,
            "quality_managed": True,
            "empty_state": True,
            "has_actual_checks": False,
            "批次产出数据": "否",
            "空态原因": EMPTY_REASON,
        }
        results.append(report)
    else:
        report = existing_report
        report_id = parse_int(report.get("id"))

    rules = store.rows("quality_rule")
    if not any(str(r.get("实体编码", "")) == entity for r in rules):
        rule_id = _next_id(rules)
        rules.append({
            "id": rule_id,
            "status": "已启用",
            "pending": False,
            "abnormal": False,
            "规则名称": "空报告迟到回执演练规则",
            "实体编码": entity,
            "检查维度": "完整性",
            "阈值口径": "0.9",
            "严重级别": "高",
        })

    receipt_result: dict[str, Any] | None = None
    if with_receipt:
        receipt_result = register_receipt({
            "entity": entity,
            "round": round_value,
            "total": 10,
            "hits": 1,
            "interrupt": bool(interrupt),
            "receipt_no": f"QRC-LATE-{entity}",
            "run_no": f"QRUN-LATE-{entity}",
        })

    inspect_report(report)
    return {
        "ok": True,
        "entity": entity,
        "round": round_value,
        "report_id": report_id,
        "report_no": report_no_of(report),
        "run_no": (receipt_result or {}).get("run_no"),
        "receipt": receipt_result,
        "message": "中断演练场景已布置：空报告已就绪，10 检 1 中的迟到回执已到规则库"
                   + ("，推送处于中断状态" if interrupt else "，推送正常"),
    }


# ---------------------------------------------------------------------------
# 台账资源服务：让运行账目/确认记录也能走通用 /api/{module} 查询
# ---------------------------------------------------------------------------

from app.services.base import BaseModuleService  # noqa: E402


class QualityCheckRunService(BaseModuleService):
    MODULE = RUN_TABLE
    ENTITY = "质量检查运行"
    LIST_FIELDS = ["规则运行号", "回执编号", "实体编码", "轮次", "检查总数", "命中数", "推送状态"]
    REQUIRED_FIELDS = ["实体编码"]
    STATUS_ORDER = ["待推送", "已完成", "已确认"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        # 走统一的幂等回执/运行登记，避免通用 POST 绕过同轮依据与去重。
        missing = [f for f in self.REQUIRED_FIELDS
                   if values.get(f) in (None, "") and f != "实体编码"]
        entity = str(values.get("实体编码") or values.get("entity") or "").strip()
        if not entity:
            return None, ["实体编码"]
        payload = dict(values)
        payload.setdefault("entity", entity)
        result = register_receipt(payload)
        if not result.get("ok"):
            return None, [str(result.get("message"))]
        run = find_run(str(result.get("run_no")))
        return run, missing


class QualityConfirmationService(BaseModuleService):
    MODULE = CONFIRM_TABLE
    ENTITY = "质量有效确认"
    LIST_FIELDS = ["确认编号", "规则运行号", "回执编号", "报告编号", "实体编码", "轮次", "评分结果"]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = ["已确认"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []
