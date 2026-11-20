from ...shared.db.model import Model
from .model import SCHEMA


class WorkspaceService(Model):
    """Add business rules using before_create/before_update/etc hooks."""
    def __init__(self, store):
        super().__init__(store, 'workspace', SCHEMA)
