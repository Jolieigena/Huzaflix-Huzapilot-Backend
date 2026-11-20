from ...shared.http.model_router import model_router
from .service import WorkspaceService


def register(app, store):
    service = WorkspaceService(store)
    app.include_router(model_router(service))
    return service
