"""质量规则库领域服务：管理主数据质量检查规则、阈值口径、严重级别与命中统计。

数据链路位置：质量治理层规则端，负责消费清洗产物执行检查，
把命中结果派发给质量报告模块。

本次修复要点：
- 收到的检查/迟到回执先在规则库登记独立运行账目与同轮依据，推送中断也可查；
- 同一回执重复送达幂等处理，只产生一次有效确认；
- total=0 的派发不再生成 1.00 满分报告，改为空态。
"""
from __future__ import annotations

import json
from typing import Any

from app.services import quality_flow as qf
from app.services.base import BaseModuleService
from app.store import store


class QualityRuleService(BaseModuleService):
    MODULE = "quality_rule"
    ENTITY = "质量规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "规则名称", "实体编码", "检查维度", "阈值口径", "严重级别",
        "最新同轮依据", "同轮",
    ]
    REQUIRED_FIELDS = ["规则名称", "实体编码", "检查维度"]
    STATUS_ORDER = ["草稿", "已启用", "已停用"]
    ACTION_RULES = {"启用规则": "已启用", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    # ---- 迟到回执 / 同轮依据 / 运行账目 ----

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        # 兼容直接通过通用 POST /api/quality_rule 送达“带检查结果的回执”的调用：
        # 只要带了运行号/回执号或检查总数，就同步登记同轮运行账目与依据。
        receipt_like = any(
            values.get(k) not in (None, "")
            for k in ("规则运行号", "run_no", "回执编号", "receipt_no", "检查总数", "total")
        )
        if receipt_like:
            payload = dict(values)
            payload.setdefault("rule_id", entry.get("id"))
            payload.setdefault("entity", entry.get("实体编码"))
            result = qf.register_receipt(payload)
            if result.get("run_no"):
                entry["最新同轮依据"] = (result.get("run") or {}).get("同轮依据", "")
        return entry, []

    def run_action(
        self, entry_id: int, action: str, remark: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        if action in ("登记回执", "收到回执", "登记迟到回执", "接收回执",
                      "登记同轮依据", "receive_receipt", "register_receipt"):
            payload = self._payload_from_remark(remark)
            rule = store.find(self.MODULE, entry_id)
            if rule is None:
                return None, f"质量规则 {entry_id} 不存在"
            payload.setdefault("rule_id", entry_id)
            payload.setdefault("entity", rule.get("实体编码"))
            result = qf.register_receipt(payload)
            if not result.get("ok"):
                return None, str(result.get("message") or "回执登记失败")
            fresh = store.find(self.MODULE, entry_id)
            return fresh, str(result.get("message") or "回执已登记")

        if action in ("中断推送", "模拟推送中断"):
            qf.set_push_interrupted(True)
            entry = store.find(self.MODULE, entry_id)
            return entry, "已模拟规则库向报告推送中断，运行账目仍可独立查询"

        if action in ("恢复推送", "恢复回执并重试", "重试推送"):
            qf.set_push_interrupted(False)
            result = qf.retry_receipt(payload=self._payload_from_remark(remark))
            entry = store.find(self.MODULE, entry_id)
            return entry, str(result.get("message") or "推送已恢复并重试")

        return super().run_action(entry_id, action, remark)

    @staticmethod
    def _payload_from_remark(remark: str | None) -> dict[str, Any]:
        if not remark:
            return {}
        text = remark.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
        # 非 JSON 备注只当运行号/回执号使用。
        if "=" in text or "：" in text or ":" in text:
            payload: dict[str, Any] = {}
            for part in text.replace("；", ";").split(";"):
                sep = "=" if "=" in part else ("：" if "：" in part else ":")
                if sep in part:
                    key, value = part.split(sep, 1)
                    payload[key.strip()] = value.strip()
            return payload
        return {"run_no": text}

    def register_late_receipt(self, payload: dict[str, Any]) -> dict[str, Any]:
        return qf.register_receipt(payload)

    def runs(self, entity: str | None = None, round_value: Any = None) -> list[dict[str, Any]]:
        return [qf._public_run(r) for r in qf.list_runs(entity=entity, round_value=round_value)]

    def get_run(self, run_no: str) -> dict[str, Any] | None:
        run = qf.find_run(run_no)
        return qf._public_run(run) if run else None

    # ---- 原有业务扩展 ----

    def check_threshold(self, entry_id: int, value: float) -> dict[str, Any]:
        """按阈值口径判定命中，严重级别决定处置。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "质量规则不存在"}
        raw = str(entry.get("阈值口径", "0.9")).rstrip("%")
        try:
            threshold = float(raw)
        except ValueError:
            threshold = 0.9
        hit = value < threshold
        if hit:
            entry["今日命中"] = str(int(entry.get("今日命中", "0") or 0) + 1)
        return {"ok": True, "hit": hit, "threshold": threshold,
                "severity": entry.get("严重级别", "中"), "value": value}

    def run_rule_batch(self, entry_id: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """批量执行质量规则，输出命中的记录集合。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已启用":
            return {"ok": False, "message": "规则未启用", "hits": []}
        dimension = str(entry.get("检查维度", ""))
        hits = [r for r in rows if not str(r.get(dimension, "")).strip()]
        entry["今日命中"] = str(int(entry.get("今日命中", "0") or 0) + len(hits))
        return {"ok": True, "hits": hits, "checked": len(rows), "dimension": dimension}

    def dispatch_result_report(
        self, entry_id: int, hit_count: int, total: int,
        round_value: Any = None, run_no: str | None = None,
    ) -> int:
        """派发质量报告生成任务到 quality_result 模块。

        total=0 时不再写 1.00 满分，而是落一条空态、等待同轮回执的报告。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return 0
        results = store.rows("quality_result")
        report_id = max((int(r.get("id", 0)) for r in results), default=0) + 1
        entity = entry.get("实体编码", "")
        round_value = qf.norm_round(round_value or entry.get("轮次"))
        total_i = qf.parse_int(total)
        hits_i = qf.parse_int(hit_count)

        report: dict[str, Any] = {
            "id": report_id,
            "报告批次": f"qr-{entry.get('规则名称')}-{report_id}",
            "报告编号": f"QRPT-{report_id:04d}",
            "实体编码": entity,
            "轮次": round_value,
            "检查总数": str(total_i),
            "命中数": str(hits_i),
            "status": "生成中" if total_i <= 0 else "已生成",
            "pending": total_i <= 0,
            "abnormal": hits_i > 0,
            "quality_managed": True,
            "规则运行号": run_no or "",
        }
        if total_i <= 0:
            report["评分结果"] = "空态·暂无评分"
            report["empty_state"] = True
            report["空态原因"] = qf.EMPTY_REASON
        else:
            _, score_text = qf.score_pair(total_i, hits_i)
            report["评分结果"] = score_text
            report["has_actual_checks"] = True
            report["批次产出数据"] = "是"
            report["计分理由"] = (
                f"同一实体 {entity}、同一轮次 {round_value}："
                f"实际检查 {total_i} 项，命中 {hits_i} 项，得分 {score_text}。"
            )
        results.append(report)
        qf.inspect_report(report)
        if total_i > 0:
            qf.push_quality_metric(report)
        return report_id

    def consume_cleanse_output(self, entry_id: int) -> list[str]:
        """读取清洗模块输出的数据集作为检查输入。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return []
        return [r.get("规则名称", "") for r in store.rows("std_cleanse")
                if r.get("实体编码") == entry.get("实体编码") and r.get("status") == "已启用"]

    def disable_with_reason(self, entry_id: int, reason: str) -> str:
        """停用规则并登记停用原因，防止误启用。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return "质量规则不存在"
        entry["status"] = "已停用"
        entry["abnormal"] = True
        entry["停用原因"] = reason
        return f"规则 {entry.get('规则名称')} 已停用: {reason}"

    def align_severity_order(self, entry_id: int, min_severity: str) -> bool:
        """校准严重级别顺序，确保告警口径统一。"""
        order = {"低": 0, "中": 1, "高": 2}
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return False
        current = str(entry.get("严重级别", "中"))
        if order.get(current, 1) < order.get(min_severity, 1):
            entry["严重级别"] = min_severity
            return True
        return False
