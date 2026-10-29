"""Authenticated cache namespaces; caching cannot establish authority."""
import json
import uuid

from fastapi.responses import JSONResponse
from unison_common.idempotency import extract_idempotency_key
from unison_common.idempotency_middleware import IdempotencyMiddleware


class PrincipalIdempotencyMiddleware(IdempotencyMiddleware):
    async def dispatch(self, request, call_next):
        key = extract_idempotency_key(dict(request.headers))
        if key:
            principal = getattr(request.state, "principal_context", None)
            if principal is None:
                return JSONResponse({"detail": "Authenticated cache context required"}, status_code=403)
            namespace = json.dumps([principal.principal_id, principal.person_id,
                                    principal.cache_namespace, request.method, request.url.path, key])
            scoped = str(uuid.uuid5(uuid.NAMESPACE_URL, namespace))
            request.scope["headers"] = [
                (name, value) for name, value in request.scope["headers"]
                if name.lower() not in (b"idempotency-key", b"x-idempotency-key")
            ] + [(b"idempotency-key", scoped.encode("ascii"))]
            # Starlette caches Headers separately from scope.
            if hasattr(request, "_headers"):
                del request._headers
        response = await super().dispatch(request, call_next)
        if key:
            response.headers["Idempotency-Key"] = key
        return response
