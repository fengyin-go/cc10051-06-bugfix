"""业务路由汇总：质量续办专用路由在前，通用资源路由在后兜底。"""
from __future__ import annotations

from app.routers import quality_flow, resource

# 注意：quality_flow 里有 /api/quality_rule/runs 这类两段路径，
# 必须先于 resource 的 /api/{module}/{entry_id} 注册，否则会被当成 entry_id。
ROUTERS = [quality_flow, resource]
