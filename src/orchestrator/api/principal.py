"""Person-scoped ingress uses authority established by authentication middleware."""
from fastapi import HTTPException, Request

from unison_common.principal import PrincipalContext, PrincipalKind, assert_identity_hints
from unison_common.principal_middleware import get_bound_principal


async def require_bound_person(request: Request) -> PrincipalContext:
    try:
        principal = get_bound_principal(request)
    except RuntimeError:
        raise HTTPException(status_code=403, detail="Authenticated person context required") from None
    # Workload delegation requires its own verified contract; a person hint is
    # insufficient to grant a service access to personal ingress/history.
    if principal.principal_kind != PrincipalKind.PERSON or not principal.person_id:
        raise HTTPException(status_code=403, detail="Authenticated person context required")
    # Validate parsed JSON independently of MIME spelling and of common's
    # middleware version. Nested identities cannot become downstream authority.
    try:
        pending = [await request.json()]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from None
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            assert_identity_hints(principal, value)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return principal
