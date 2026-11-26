"""质量链路专用接口：迟到回执、检查运行账目、报告续办/状态绑定下载、看板依据。

通用资源路由 /api/{module} 之外，这里补齐题面场景需要的语义化接口；
同一能力尽量同时挂多个自然路径别名，方便不同调用方按约定接入。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.services import quality_flow as qf
from app.store import store

router = APIRouter(prefix="/api", tags=["质量报告续办与同轮回执"])


async def _payload(request: Request) -> dict[str, Any]:
    """合并 JSON body、表单与 query 参数，query 优先级最低。"""
    data: dict[str, Any] = {}
    if request.method == "POST":
        ctype = request.headers.get("content-type", "")
        try:
            if "application/json" in ctype:
                body = await request.json()
                if isinstance(body, dict):
                    # 兼容 {values: {...}} / {payload: {...}} 包装。
                    data.update(body)
                    for wrapper in ("values", "payload", "data"):
                        inner = body.get(wrapper)
                        if isinstance(inner, dict):
                            merged = dict(inner)
                            merged.update({k: v for k, v in body.items() if k != wrapper})
                            data = merged
                            break
                elif isinstance(body, list):
                    data["checks"] = body
            elif "form" in ctype:
                form = await request.form()
                data.update({k: v for k, v in form.items()})
        except Exception:
            pass
    data.update({k: v for k, v in request.query_params.items() if k not in data})
    return data


def _attachment(content: str, filename: str, media_type: str) -> Response:
    encoded = quote(filename)
    return Response(
        content=content.encode("utf-8"),
        media_type=media_type,
        headers={
            "Content-Disposition":
                f"attachment; filename=\"{encoded}\"; filename*=UTF-8''{encoded}",
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# 迟到回执登记（质量规则库侧）
# ---------------------------------------------------------------------------

_RECEIPT_PATHS = (
    "/quality_rule/receipts",
    "/quality_rule/receipt",
    "/quality_rule/late-receipt",
    "/quality_rule/receive-receipt",
    "/quality_receipts",
    "/quality_receipt",
    "/quality/receipts",
    "/receipts/quality",
)


@router.post("/quality_rule/{rule_id:int}/receipts")
async def register_rule_receipt(rule_id: int, request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    payload.setdefault("rule_id", rule_id)
    rule = store.find("quality_rule", rule_id)
    if rule is not None:
        payload.setdefault("entity", rule.get("实体编码"))
    return qf.register_receipt(payload)


for _path in _RECEIPT_PATHS:
    router.add_api_route(
        _path, qf.register_receipt, methods=["POST"],
        name=f"receipt_{_path}",
    )


# 让直接挂函数时 request 可注入：包一层。
def _register_receipt_entry(payload: dict[str, Any]) -> dict[str, Any]:
    return qf.register_receipt(payload)


# ---------------------------------------------------------------------------
# 检查运行账目独立查询（推送中断时仍可查）
# ---------------------------------------------------------------------------

@router.get("/quality_rule/runs")
@router.get("/quality_check_runs")
@router.get("/quality-runs")
@router.get("/quality_run")
async def list_quality_runs(request: Request) -> dict[str, Any]:
    entity = request.query_params.get("entity") or request.query_params.get("实体编码")
    round_value = request.query_params.get("round") or request.query_params.get("轮次")
    run_no = request.query_params.get("run_no") or request.query_params.get("规则运行号")
    rows = [qf._public_run(r) for r in qf.list_runs(entity, round_value, run_no)]
    return {"ok": True, "items": rows, "total": len(rows)}


@router.get("/quality_rule/runs/{run_no}")
@router.get("/quality_rule/run/{run_no}")
@router.get("/quality-runs/{run_no}")
@router.get("/quality/runs/{run_no}")
@router.get("/runs/{run_no}")
async def get_quality_run(run_no: str) -> dict[str, Any]:
    run = qf.find_run(run_no)
    if run is None:
        return JSONResponse(status_code=404,
                            content={"ok": False, "message": f"规则运行号 {run_no} 不存在"})
    return {"ok": True, "run": qf._public_run(run)}


@router.get("/quality_rule/confirmations")
@router.get("/quality_confirmations")
@router.get("/quality_confirmation")
async def list_confirmations() -> dict[str, Any]:
    rows = list(store.rows(qf.CONFIRM_TABLE))
    return {"ok": True, "items": rows, "total": len(rows)}


@router.get("/quality_receipts")
@router.get("/quality_receipt")
@router.get("/quality_rule/receipts")
async def list_receipts() -> dict[str, Any]:
    items = [
        {
            "回执编号": r.get("回执编号"),
            "规则运行号": r.get("规则运行号"),
            "实体编码": r.get("实体编码"),
            "轮次": qf.norm_round(r.get("轮次")),
            "检查总数": qf.parse_int(r.get("检查总数")),
            "命中数": qf.parse_int(r.get("命中数")),
            "deliveries": qf.parse_int(r.get("deliveries"), 1),
            "推送状态": r.get("推送状态"),
            "有效确认次数": qf.parse_int(r.get("有效确认次数"), 0),
            "received_at": r.get("created_at"),
        }
        for r in store.rows(qf.RUN_TABLE)
    ]
    return {"ok": True, "items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# 推送中断 / 恢复 / 重试
# ---------------------------------------------------------------------------

@router.post("/quality_rule/push/interrupt")
@router.post("/quality/push/interrupt")
async def interrupt_push(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    cfg = qf.set_push_interrupted(True)
    if payload.get("run_no"):
        run = qf.find_run(str(payload["run_no"]))
        if run is not None:
            run["推送状态"] = "中断"
    return {"ok": True, "push_interrupted": True, "control": cfg,
            "message": "已中断规则库向报告的推送，检查运行账目仍可独立查询"}


@router.post("/quality_rule/push/restore")
@router.post("/quality_rule/push/recover")
@router.post("/quality/push/restore")
async def restore_push(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    result = qf.retry_receipt(payload=payload)
    result["push_interrupted"] = False
    return result


@router.post("/quality_rule/push/retry")
@router.post("/quality_receipts/retry")
async def retry_push_all(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    return qf.retry_receipt(payload=payload)


@router.post("/quality_rule/runs/{run_no}/retry")
@router.post("/quality_rule/runs/{run_no}/restore")
async def retry_one_run(run_no: str, request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    return qf.retry_receipt(run_no, payload)


# ---------------------------------------------------------------------------
# 报告续办
# ---------------------------------------------------------------------------

def _resolve_report_ref(report_id: Any = None, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """按数字 id 或报告编号/报告批次定位报告。"""
    payload = payload or {}
    rid = qf.parse_int(report_id, 0) or qf.parse_int(qf.pick(payload, "report_id", 0), 0)
    if rid:
        found = store.find("quality_result", rid)
        if found is not None:
            return found
    no = str(qf.pick(payload, "report_no", "") or "").strip()
    if no:
        for r in store.rows("quality_result"):
            if str(r.get("报告编号", "")) == no or str(r.get("报告批次", "")) == no:
                return r
    return None


@router.post("/quality_result/{report_id:int}/continue")
@router.post("/quality_result/{report_id:int}/resume")
@router.post("/quality_result/{report_id:int}/reconcile")
@router.post("/quality_result/{report_id:int}/handle")
async def continue_report(report_id: int, request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    return qf.continue_report(report_id, payload)


@router.get("/quality_result/{report_id:int}/continue")
@router.get("/quality_result/{report_id:int}/reconcile")
async def continue_report_get(report_id: int, request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    return qf.continue_report(report_id, payload)


@router.post("/quality_result/continue")
async def continue_report_by_body(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    report = _resolve_report_ref(payload=payload)
    if report is None:
        return {"ok": False, "message": "缺少可定位的 report_id / report_no"}
    return qf.continue_report(qf.parse_int(report.get("id")), payload)


@router.post("/quality_result/{report_id:int}/push-stats")
async def push_stats(report_id: int) -> dict[str, Any]:
    service = __import__(
        "app.services.quality_result", fromlist=["QualityResultService"]
    ).QualityResultService()
    stat_id = service.push_stats_overview(report_id)
    return {"ok": bool(stat_id), "stat_id": stat_id}


@router.get("/quality_result/{report_id:int}/availability")
@router.get("/quality_result/{report_id:int}/status")
@router.get("/quality_result/{report_id:int}/inspect")
async def report_availability(report_id: int) -> dict[str, Any]:
    report = store.find("quality_result", report_id)
    if report is None:
        return JSONResponse(status_code=404, content={"ok": False, "message": "报告不存在"})
    projection = qf.present_report(report)
    return {
        "ok": True,
        "report_id": report_id,
        "availability_code": projection.get("availability_code"),
        "可用状态": report.get("可用状态", ""),
        "available": projection.get("available"),
        "empty": projection.get("empty"),
        "qualified": report.get("qualified"),
        "run_no": projection.get("run_no", ""),
        "candidate_run_nos": projection.get("candidate_run_nos", []),
        "报告编号": qf.report_no_of(report),
        "轮次": qf.report_round(report),
        "entity": qf.report_entity(report),
        "评分结果": report.get("评分结果", ""),
        "计分理由": report.get("计分理由", ""),
        "空态原因": report.get("空态原因", ""),
        "有效确认次数": qf.parse_int(report.get("有效确认次数"), 0),
    }


@router.post("/quality_result/{report_id:int}/generate")
@router.post("/quality_result/generate/{report_id:int}")
async def generate_report_alias(report_id: int, request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    service = __import__(
        "app.services.quality_result", fromlist=["QualityResultService"]
    ).QualityResultService()
    result = service.generate_report(report_id)
    entry = qf.present_report(store.find("quality_result", report_id) or {})
    result["entry"] = entry
    if payload.get("push"):
        result["stat_id"] = service.push_stats_overview(report_id)
    return result


@router.post("/quality_result/{report_id:int}/retry")
@router.post("/quality_result/retry/{report_id:int}")
async def retry_report_alias(report_id: int, request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    service = __import__(
        "app.services.quality_result", fromlist=["QualityResultService"]
    ).QualityResultService()
    hint = str(payload.get("fix_hint") or payload.get("reason") or "接口重试")
    if store.find("quality_result", report_id) is not None and \
            store.find("quality_result", report_id).get("status") == "生成失败":
        result = service.retry_generate(report_id, hint)
    else:
        result = qf.continue_report(report_id, payload)
    entry = qf.present_report(store.find("quality_result", report_id) or {})
    result["entry"] = entry
    return result


# ---------------------------------------------------------------------------
# 状态绑定的报告下载
# ---------------------------------------------------------------------------

@router.get("/quality_result/download")
async def download_by_query(request: Request) -> Response:
    report = _resolve_report_ref(
        request.query_params.get("report_id"),
        {"report_no": request.query_params.get("report_no") or request.query_params.get("报告编号")},
    )
    if report is None:
        return JSONResponse(status_code=400,
                            content={"ok": False, "message": "缺少 report_id / report_no"})
    return await download_report(qf.parse_int(report.get("id")), request)


@router.get("/quality_result/{report_id:int}/download")
@router.get("/quality_result/{report_id:int}/report")
@router.get("/quality_result/{report_id:int}/markdown")
@router.get("/quality_result/{report_id:int}/export-report")
@router.get("/quality_result/{report_id:int}/export")
async def download_report(report_id: int, request: Request) -> Response:
    report = store.find("quality_result", report_id)
    if report is None:
        return JSONResponse(status_code=404,
                            content={"ok": False, "message": "报告不存在"})
    run_no = request.query_params.get("run_no") or request.query_params.get("规则运行号")
    if run_no:
        result = qf.continue_report(report_id, {"run_no": run_no, "source": "download"})
        if not result.get("ok") and not result.get("waiting"):
            return JSONResponse(status_code=409, content=result)
        report = store.find("quality_result", report_id)

    fmt = request.query_params.get("format", "md")
    wants_json = fmt == "json" or "application/json" in request.headers.get("accept", "") and fmt != "csv"
    if wants_json:
        projection = qf.inspect_report(report)
        content, filename, _ = qf.render_report_download(report, fmt="md")
        return JSONResponse(content={
            "ok": True, "filename": filename, "content": content,
            "report": {k: v for k, v in report.items() if k != "检查明细"},
            "projection": projection,
        })

    content, filename, media_type = qf.render_report_download(
        report, fmt="csv" if fmt == "csv" else "md"
    )
    if fmt == "txt":
        media_type = "text/plain; charset=utf-8"
        filename = filename.rsplit(".", 1)[0] + ".txt"
    return _attachment(content, filename, media_type)


# ---------------------------------------------------------------------------
# 治理看板：质量指标依据
# ---------------------------------------------------------------------------

@router.get("/stats_overview/quality-basis")
@router.get("/stats_overview/basis")
@router.get("/stats_overview/quality-metrics")
@router.get("/stats_overview/quality_basis")
async def quality_basis() -> dict[str, Any]:
    result = qf.quality_basis_overview()
    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# 场景布置（一键造出 prompt 中的中断现场）
# ---------------------------------------------------------------------------

@router.post("/quality_flow/scenario")
@router.post("/quality/scenario")
@router.post("/quality_result/scenario")
async def bootstrap_scenario(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    return qf.bootstrap_scenario(payload)


@router.get("/quality_flow/scenario")
async def bootstrap_scenario_get() -> dict[str, Any]:
    return qf.bootstrap_scenario({})
