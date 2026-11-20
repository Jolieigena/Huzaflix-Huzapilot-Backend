from ...shared.http.model_router import model_router
from .service import MemberService


def register(app, store):
    service = MemberService(store)
    app.include_router(model_router(service))
    return service
