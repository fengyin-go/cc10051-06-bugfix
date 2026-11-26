"""质量报告领域服务：管理按规则维度生成的主数据质量报告、评分与失败重试。

数据链路位置：质量治理层报告端，负责消费规则命中记录，
把评分结果推送到治理看板快照。

本次修复要点：
- 空检查（检查总数 0 且无同轮实际检查）保留空态与原因，不再算成 100% 满分；
- 只有同一实体、同一轮次的检查运行才能汇入报告；
- 续办可凭规则运行号主动拉取中断推送背后的真实结果，确认全程幂等。
"""
from __future__ import annotations

import json
from typing import Any

from app.services import quality_flow as qf
from app.services.base import BaseModuleService
from app.store import store


class QualityResultService(BaseModuleService):
    MODULE = "quality_result"
    ENTITY = "质量结果"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "报告批次", "实体编码", "检查总数", "命中数", "评分结果",
        "报告编号", "轮次", "可用状态", "规则运行号",
        "空态原因", "计分理由", "批次产出数据",
    ]
    REQUIRED_FIELDS = ["报告批次", "实体编码", "检查总数"]
    STATUS_ORDER = ["生成中", "已生成", "生成失败"]
    ACTION_RULES = {"生成报告": "已生成", "重试生成": "生成失败"}
    NEGATIVE_ACTIONS = []

    # ---- 查询：输出前由服务端判定可用状态 ----

    def list_entries(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        rows, total = super().list_entries(**kwargs)
        for row in rows:
            qf.present_report(row)
        return rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = super().get_entry(entry_id)
        if entry is not None:
            qf.present_report(entry)
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        # 保留 LIST_FIELDS 之外的扩展字段（轮次、规则运行号、回执编号等）。
        for key, value in values.items():
            if key == "id":
                continue
            if key not in entry or key not in self.LIST_FIELDS:
                if value is not None:
                    entry[key] = value
        for numeric_field in ("检查总数", "命中数"):
            if str(entry.get(numeric_field, "")).strip() != "":
                entry[numeric_field] = str(qf.parse_int(entry.get(numeric_field)))
        entry.setdefault("命中数", "0")
        entry["quality_managed"] = True
        entry["轮次"] = qf.norm_round(entry.get("轮次"))
        qf.report_no_of(entry)
        entry["total"] = qf.parse_int(entry.get("检查总数"))
        entry["hits"] = qf.parse_int(entry.get("命中数"))
        entry["有效确认次数"] = qf.parse_int(entry.get("有效确认次数"), 0)
        qf.inspect_report(entry)
        # 新建即空报告时，状态保持“生成中”，不能直接变成“已生成/满分”。
        if entry.get("empty"):
            entry["status"] = "生成中"
            entry["pending"] = True
        return entry, []

    # ---- 动作分派：生成 / 续办 / 重试 / 下载信息 ----

    def run_action(
        self, entry_id: int, action: str, remark: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        payload = self._parse_remark(remark)

        if action in ("续办", "继续办理", "报告续办", "续办办理", "continue", "resume", "reconcile"):
            if not payload.get("run_no") and remark and not self._looks_like_json(remark):
                payload["run_no"] = remark.strip()
            result = qf.continue_report(entry_id, payload)
            fresh = qf.present_report(store.find(self.MODULE, entry_id))
            if result.get("ok") and result.get("confirmed"):
                return fresh, str(result.get("message") or "报告续办完成，已汇入同轮检查结果")
            if result.get("waiting"):
                # 仍在等待回执：动作受理，但报告不变成已生成。
                return fresh, str(result.get("message") or "仍待规则回执")
            return None, str(result.get("message") or "续办失败")

        if action in ("下载报告", "下载", "报告下载"):
            qf.present_report(entry)
            download_path = f"/api/quality_result/{entry_id}/download"
            entry["下载地址"] = download_path
            return entry, f"报告下载已准备：{download_path}"

        if action in ("生成报告",):
            result = self.generate_report(entry_id)
            fresh = qf.present_report(store.find(self.MODULE, entry_id))
            if not result.get("ok"):
                return None, str(result.get("message") or "报告生成失败")
            return fresh, str(result.get("message") or "质量报告已生成")

        if action in ("重试生成", "重试"):
            return self._retry_action(entry_id, payload)

        return super().run_action(entry_id, action, remark)

    @staticmethod
    def _looks_like_json(text: str) -> bool:
        stripped = (text or "").strip()
        return stripped.startswith("{") or stripped.startswith("[")

    @staticmethod
    def _parse_remark(remark: str | None) -> dict[str, Any]:
        if not remark:
            return {}
        text = remark.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                return {}
        # 兼容 “规则运行号=QRUN-1” / “QRUN-1” 这类简单备注。
        payload: dict[str, Any] = {}
        for part in text.replace("；", ";").split(";"):
            if "=" in part or "：" in part or ":" in part:
                sep = "=" if "=" in part else ("：" if "：" in part else ":")
                key, value = part.split(sep, 1)
                payload[key.strip()] = value.strip()
        if not payload:
            payload["run_no"] = text
        return payload

    def _retry_action(
        self, entry_id: int, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        assert entry is not None
        # 失败重试保留原语义：先回到生成中。
        if entry.get("status") == "生成失败":
            entry["status"] = "生成中"
            entry["pending"] = True
            if payload.get("fix_hint"):
                entry["修复说明"] = payload["fix_hint"]
        # 重试同时做一次同轮续办，让迟到回执有机会接通（幂等）。
        result = qf.continue_report(entry_id, payload)
        fresh = qf.present_report(store.find(self.MODULE, entry_id))
        if result.get("confirmed"):
            return fresh, str(result.get("message") or "重试成功，已汇入真实检查结果")
        generated = self.generate_report(entry_id)
        fresh = qf.present_report(store.find(self.MODULE, entry_id))
        if not generated.get("ok"):
            return None, str(generated.get("message") or "重试生成失败")
        return fresh, str(generated.get("message") or "已重新生成报告")

    # ---- 业务扩展：质量报告生成与看板推送 ----

    def generate_report(self, entry_id: int) -> dict[str, Any]:
        """汇总规则命中生成质量报告，计算评分。

        空态口径：检查总数为 0 且没有同轮实际检查时，不打分、不置为已生成，
        只留下空态与原因；确实执行了全部检查且 0 命中时，仍为 100%。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "质量报告不存在"}

        # 若已有同轮运行账目（含中断推送的），生成时先尝试凭唯一候选汇入。
        if qf.parse_int(entry.get("检查总数")) == 0 and not entry.get("has_actual_checks"):
            candidates = qf.candidate_runs_for_report(entry)
            if len(candidates) == 1 and qf.parse_int(candidates[0].get("检查总数")) > 0:
                confirmed = qf.confirm_run(candidates[0], entry, source="generate")
                if confirmed.get("ok") and confirmed.get("created"):
                    return {
                        "ok": True, "score": entry.get("评分结果"),
                        "total": qf.parse_int(entry.get("检查总数")),
                        "hits": qf.parse_int(entry.get("命中数")),
                        "merged_run": candidates[0].get("规则运行号"),
                        "message": confirmed.get("message"),
                    }

        total = qf.parse_int(entry.get("检查总数"))
        hits = qf.parse_int(entry.get("命中数"))
        if hits > total and total > 0:
            entry["status"] = "生成失败"
            entry["abnormal"] = True
            entry["pending"] = False
            return {"ok": False, "message": "命中数超过检查总数，数据异常"}

        entry["轮次"] = qf.norm_round(entry.get("轮次"))
        qf.report_no_of(entry)

        if total <= 0 and not entry.get("has_actual_checks"):
            # 关键修复：空检查不再得到 100% 满分。
            entry["检查总数"] = "0"
            entry.setdefault("命中数", "0")
            entry["empty_state"] = True
            entry["empty"] = True
            entry["available"] = False
            entry["qualified"] = False
            entry["score"] = None
            entry["score_percent"] = None
            entry["评分结果"] = "空态·暂无评分"
            entry["空态原因"] = qf.EMPTY_REASON
            entry["status"] = "生成中"
            entry["pending"] = True
            qf.inspect_report(entry)
            return {
                "ok": True, "empty": True, "is_empty": True,
                "score": None, "total": 0, "hits": 0,
                "reason": qf.EMPTY_REASON,
                "空态原因": qf.EMPTY_REASON,
                "availability_code": entry.get("availability_code"),
                "run_no": entry.get("规则运行号", ""),
                "candidate_run_nos": entry.get("候选规则运行号", []),
                "message": "暂无实际检查，已保留空态与原因，不计满分/合格",
            }

        effective_total = max(total, 1)
        score, score_text = qf.score_pair(effective_total, hits)
        entry["检查总数"] = str(total if total > 0 else effective_total)
        entry["评分结果"] = score_text
        entry["score"] = round(score, 4)
        entry["score_percent"] = round(score * 100, 2)
        entry["score_decimal"] = f"{score:.2f}"
        entry["status"] = "已生成"
        entry["pending"] = False
        entry["abnormal"] = hits > 0
        entry["empty_state"] = False
        entry["empty"] = False
        entry["available"] = True
        entry["qualified"] = hits == 0
        entry["has_actual_checks"] = True
        entry["批次产出数据"] = "是"
        if not entry.get("计分理由"):
            entry["计分理由"] = (
                f"实体 {qf.report_entity(entry)}、轮次 {qf.report_round(entry)}："
                f"实际检查 {total} 项，命中 {hits} 项，得分 {score_text}。"
            )
        # 有实际检查才给看板发质量指标。
        stat_id = qf.push_quality_metric(entry)
        return {"ok": True, "score": score_text, "total": total,
                "hits": hits, "stat_id": stat_id,
                "message": f"质量报告已生成，评分 {score_text}"}

    def retry_generate(self, entry_id: int, fix_hint: str) -> dict[str, Any]:
        """失败重试：校验数据修复后重新生成；也会尝试接通同轮迟到结果。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "质量结果不存在"}
        if entry.get("status") != "生成失败":
            return {"ok": False, "message": "报告未失败，无需重试"}
        entry["status"] = "生成中"
        entry["pending"] = True
        entry["修复说明"] = fix_hint
        reconciled = qf.continue_report(entry_id, {"fix_hint": fix_hint})
        if reconciled.get("confirmed"):
            return {"ok": True, "message": "已重入生成队列并接通同轮真实结果"}
        return {"ok": True, "message": "已重入生成队列"}

    def consume_rule_hits(self, entry_id: int) -> list[dict[str, Any]]:
        """读取质量规则库中同一实体、同一轮次的检查运行/命中记录。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return []
        return qf.candidate_runs_for_report(entry)

    def push_stats_overview(self, entry_id: int) -> int:
        """把评分结果推送到治理看板快照；空报告不推，同一报告不重复推。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return 0
        qf.inspect_report(entry)
        if entry.get("empty") or (
            qf.parse_int(entry.get("检查总数")) <= 0
            and not entry.get("has_actual_checks")
        ):
            return 0
        if entry.get("status") != "已生成":
            result = self.generate_report(entry_id)
            if not result.get("ok") or result.get("empty"):
                return 0
        return qf.push_quality_metric(entry)

    def export_report_markdown(self, entry_id: int) -> str:
        """导出质量报告为 Markdown 文本（内容与服务端判定状态绑定）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return ""
        content, _, _ = qf.render_report_download(entry, fmt="md")
        return content

    def filter_by_score(self, min_score: float) -> list[dict[str, Any]]:
        """按评分阈值筛选低质量报告，供回捞重算。空态报告不参与评分筛选。"""
        result: list[dict[str, Any]] = []
        for r in store.rows(self.MODULE):
            if r.get("status") != "已生成":
                continue
            raw = str(r.get("评分结果", "")).rstrip("%")
            try:
                value = float(raw)
            except ValueError:
                continue
            if value < min_score * 100:
                result.append(r)
        return result
