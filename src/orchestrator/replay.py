from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import Response

from unison_common import require_roles
from unison_common.auth import verify_token
from unison_common.replay_store import ReplayConfig, get_replay_manager, initialize_replay

logger = logging.getLogger(__name__)


def configure_replay_store() -> None:
    """Initialize the shared replay store with default limits."""
    replay_config = ReplayConfig()
    replay_config.default_retention_days = 30
    replay_config.max_envelopes_per_trace = 1000
    replay_config.max_stored_envelopes = 50000
    initialize_replay(replay_config)
    logger.info("Replay store initialized for M3 event storage")


def register_replay_routes(app) -> None:
    router = APIRouter(prefix="/replay")

    @router.get("/traces")
    async def list_traces(
        limit: int = 50,
        offset: int = 0,
        user_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        status: Optional[str] = None,
        intent: Optional[str] = None,
        current_user: Dict[str, Any] = Depends(verify_token),
    ):
        """List stored traces with filtering and pagination."""
        replay_manager = get_replay_manager()
        start_dt = datetime.fromisoformat(start_date) if start_date else None
        end_dt = datetime.fromisoformat(end_date) if end_date else None

        filtered_ids, total_count = replay_manager.store.filter_traces(
            user_id=user_id,
            start_date=start_dt,
            end_date=end_dt,
            status=status,
            intent=intent,
            limit=limit,
            offset=offset,
        )

        traces = []
        for trace_id in filtered_ids:
            summary = replay_manager.get_trace_summary(trace_id)
            if summary.get("found"):
                traces.append(summary)

        return {
            "traces": traces,
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "filtered": len(traces),
            "filters": {
                "user_id": user_id,
                "start_date": start_date,
                "end_date": end_date,
                "status": status,
                "intent": intent,
            },
        }

    @router.get("/{trace_id}/summary")
    async def get_trace_summary(trace_id: str, current_user: Dict[str, Any] = Depends(verify_token)):
        replay_manager = get_replay_manager()
        summary = replay_manager.get_trace_summary(trace_id)
        if not summary.get("found"):
            raise HTTPException(status_code=404, detail=f"Trace {trace_id} not found")
        return summary

    @router.post("/{trace_id}")
    async def replay_trace(
        trace_id: str,
        replay_options: Dict[str, Any] = Body(default={"include_context": True, "time_scale": 1.0}),
        current_user: Dict[str, Any] = Depends(verify_token),
    ):
        replay_manager = get_replay_manager()
        summary = replay_manager.get_trace_summary(trace_id)
        if not summary.get("found"):
            raise HTTPException(status_code=404, detail=f"Trace {trace_id} not found")

        envelopes = replay_manager.store.get_envelopes_by_trace(trace_id)
        events = [env.to_dict() for env in envelopes]

        return {
            "status": "replayed",
            "trace_id": trace_id,
            "envelopes": events,
            "summary": summary,
            "replay_options": replay_options,
            "requested_by": current_user.get("username"),
            "replay_time": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }

    @router.delete("/{trace_id}")
    async def delete_trace(trace_id: str, current_user: Dict[str, Any] = Depends(require_roles(["admin"]))):
        replay_manager = get_replay_manager()
        summary = replay_manager.get_trace_summary(trace_id)
        if not summary.get("found"):
            raise HTTPException(status_code=404, detail=f"Trace {trace_id} not found")

        success = replay_manager.store.delete_trace(trace_id)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete trace")

        return {
            "status": "deleted",
            "trace_id": trace_id,
            "deleted_by": current_user.get("username"),
            "deleted_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }

    @router.get("/{trace_id}/export")
    async def export_trace(
        trace_id: str,
        format: str = "json",
        current_user: Dict[str, Any] = Depends(require_roles(["admin", "operator"])),
    ):
        replay_manager = get_replay_manager()
        summary = replay_manager.get_trace_summary(trace_id)
        if not summary.get("found"):
            raise HTTPException(status_code=404, detail=f"Trace {trace_id} not found")

        envelopes = replay_manager.store.get_envelopes_by_trace(trace_id)

        if format == "csv":
            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=[
                    "envelope_id",
                    "trace_id",
                    "correlation_id",
                    "timestamp",
                    "event_type",
                    "source",
                    "user_id",
                    "processing_time_ms",
                    "status_code",
                    "error_message",
                    "intent",
                    "payload",
                ],
            )
            writer.writeheader()
            for env in envelopes:
                writer.writerow(
                    {
                        "envelope_id": env.envelope_id,
                        "trace_id": env.trace_id,
                        "correlation_id": env.correlation_id,
                        "timestamp": env.timestamp.isoformat(),
                        "event_type": env.event_type,
                        "source": env.source,
                        "user_id": env.user_id or "",
                        "processing_time_ms": env.processing_time_ms or "",
                        "status_code": env.status_code or "",
                        "error_message": env.error_message or "",
                        "intent": env.envelope_data.get("intent", ""),
                        "payload": json.dumps(env.envelope_data.get("payload", {})),
                    }
                )
            return Response(
                content=output.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename=trace_{trace_id}.csv"},
            )

        events = [env.to_dict() for env in envelopes]
        export_data = {
            "trace_id": trace_id,
            "exported_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "exported_by": current_user.get("username"),
            "events_count": len(events),
            "events": events,
            "summary": summary,
        }
        return Response(
            content=json.dumps(export_data, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=trace_{trace_id}.json"},
        )

    @router.get("/statistics")
    async def replay_statistics(current_user: Dict[str, Any] = Depends(verify_token)):
        replay_manager = get_replay_manager()
        stats = replay_manager.store.get_statistics()

        user_roles = current_user.get("roles", [])
        if "admin" in user_roles:
            user_counts: Dict[str, int] = {}
            for trace_id in replay_manager.store.get_trace_ids():
                envelopes = replay_manager.store.get_envelopes_by_trace(trace_id)
                if envelopes and envelopes[0].user_id:
                    user_id = envelopes[0].user_id
                    user_counts[user_id] = user_counts.get(user_id, 0) + 1
            stats["traces_by_user"] = user_counts

        return {"statistics": stats, "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")}

    app.include_router(router)
