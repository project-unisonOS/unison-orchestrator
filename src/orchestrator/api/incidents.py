"""Versioned service route for the DJ-1 simulated household incident."""

from datetime import datetime

from fastapi import APIRouter, Body, HTTPException
from unison_common.contracts.v1.shared_incident import OfflineKnowledgePack, SensorObservation
from unison_common.principal_middleware import get_current_principal

from ..incident_workflow import IncidentRendererService, IncidentStorageService, SharedIncidentWorkflow
from ..incident_delivery import IncidentDeliveryOutbox
from ..shared_incident import IncidentEngineRejected, SharedIncidentEngine


def register_incident_routes(app, *, service_clients, outbox=None) -> None:
    api = APIRouter()
    delivery_outbox = outbox or IncidentDeliveryOutbox()

    @api.post("/v1/incidents/simulations/water-leak", status_code=201)
    def simulate_water_leak(body: dict = Body(...)):
        principal = get_current_principal()
        person_id = principal.person_id if principal else body.get("person_id")
        assistant_id = principal.assistant_instance_id if principal else body.get("assistant_instance_id")
        household_id = principal.household_id if principal else body.get("household_id")
        if not person_id or not assistant_id or not household_id:
            raise HTTPException(status_code=400, detail="person, assistant, and household authority required")
        space_id = f"shared:{household_id}"
        try:
            observation = SensorObservation.model_validate(body.get("observation"))
            pack = OfflineKnowledgePack.model_validate(body.get("knowledge_pack"))
            workflow = SharedIncidentWorkflow(SharedIncidentEngine(pack), IncidentStorageService(service_clients.storage))
            incident, checklist = workflow.start(
                person_id=person_id, assistant_instance_id=assistant_id, space_id=space_id,
                observation=observation, authorized_space_ids=[space_id], at=datetime.fromisoformat(body["at"]),
                electrical_hazard=body.get("electrical_hazard") is True)
            delivered = False
            if service_clients.renderer:
                renderer = IncidentRendererService(service_clients.renderer)
                delivered = renderer.publish(incident, checklist)
                if not delivered:
                    delivery_outbox.enqueue(incident.incident_id, renderer.envelope(incident, checklist))
            return {"incident": incident.model_dump(mode="json"), "checklist": checklist,
                    "renderer_delivered": delivered, "evidence_class": "simulation"}
        except (IncidentEngineRejected, TypeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.post("/v1/incidents/delivery/retry")
    def retry_incident_delivery():
        if not service_clients.renderer:
            raise HTTPException(status_code=503, detail="renderer service is unavailable")
        delivered = 0
        remaining = 0
        for path, envelope in delivery_outbox.pending():
            ok, status, _ = service_clients.renderer.post("/events", envelope)
            if ok and status == 200:
                delivery_outbox.acknowledge(path)
                delivered += 1
            else:
                remaining += 1
        return {"delivered": delivered, "remaining": remaining, "evidence_class": "simulation"}

    app.include_router(api)
