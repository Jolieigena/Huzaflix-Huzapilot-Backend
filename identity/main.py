"""Internal-only identity authority. Native gateways forward public auth routes."""

import hmac
import json
import logging
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from identity import Identity, IdentityError

logging.basicConfig(level=logging.INFO)
identity = Identity(json.load(open("scopes.json")))
app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
COOKIE = "huza_refresh"
origins = set(os.environ["BROWSER_ORIGINS"].split(","))
secure = os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true"


from app.shared.monitoring import generate_latest, CONTENT_TYPE_LATEST
from app.shared.middleware.context import RequestContextMiddleware
app.add_middleware(RequestContextMiddleware)

@app.get('/metrics')
def metrics(request: Request):
    token=os.getenv('MONITORING_TOKEN','')
    if token and not hmac.compare_digest(request.headers.get('authorization',''),'Bearer '+token):
        return Response(status_code=401)
    return Response(generate_latest(),headers={'Content-Type':CONTENT_TYPE_LATEST})

@app.get("/health")
def health():
    with identity.lock:
        identity.db.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def dispatch(request: Request, path: str):
    if not hmac.compare_digest(
        request.headers.get("x-huza-internal-token", ""),
        os.environ["API_KEY_AUTH_SECRET"],
    ):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    browser = False
    try:
        raw = await request.body()
        if len(raw) > 65536:
            raise IdentityError(413, "Request too large")
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise IdentityError(400, "Invalid JSON")
        if not isinstance(body, dict):
            raise IdentityError(400, "JSON object required")
        if path == "authorize" and request.method == "POST":
            result = await run_in_threadpool(
                identity.authorize, body.get("token", ""), body.get("scope", "")
            )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        if not path.startswith("auth/"):
            raise IdentityError(404, "Not found")
        if request.headers.get("x-api-key") is not None:
            raise IdentityError(401, "Account credentials required")
        browser = body.pop("browser", False) is True
        if browser:
            if request.headers.get("origin") not in origins:
                raise IdentityError(403, "Untrusted browser origin")
            if path in ("auth/refresh", "auth/logout"):
                body["refresh_token"] = request.cookies.get(COOKIE, "")
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        status, result = await run_in_threadpool(
            identity.handle,
            request.method,
            "/" + path,
            body,
            token,
            request.headers.get("x-huza-peer", "unknown"),
        )
        refresh = result.pop("refresh_token", None) if browser else None
        response = JSONResponse(
            result,
            status_code=status,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
        if refresh:
            response.set_cookie(
                COOKIE,
                refresh,
                httponly=True,
                secure=secure,
                samesite="strict",
                path="/auth",
                max_age=604800,
            )
        if browser and path == "auth/logout":
            response.delete_cookie(
                COOKIE, path="/auth", httponly=True, secure=secure, samesite="strict"
            )
        return response
    except IdentityError as e:
        return JSONResponse(
            {"error": e.message},
            status_code=e.status,
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        logging.exception("Identity operation failed")
        return JSONResponse(
            {"error": "Identity authority unavailable"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
