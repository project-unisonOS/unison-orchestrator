"""Person-scoped ingress uses authority established by authentication middleware."""
from fastapi import HTTPException, Request

from unison_common.principal import PrincipalContext, PrincipalKind
from unison_common.principal_middleware import get_bound_principal


def require_bound_person(request: Request) -> PrincipalContext:
    try:
        principal = get_bound_principal(request)
    except RuntimeError:
        raise HTTPException(status_code=403, detail="Authenticated person context required") from None
    # Workload delegation requires its own verified contract; a person hint is
    # insufficient to grant a service access to personal ingress/history.
    if principal.principal_kind != PrincipalKind.PERSON or not principal.person_id:
        raise HTTPException(status_code=403, detail="Authenticated person context required")
    return principal
