import json

SCHEMA = json.loads('{"type": "object", "properties": {"name": {"type": "string"}, "serviceName": {"type": "string", "default": "workspaces", "example": "workspaces", "description": "Editable initial-generation metadata; remove when no longer needed."}, "modelName": {"type": "string", "default": "workspace", "example": "workspace", "description": "Editable initial-generation metadata; remove when no longer needed."}}, "required": ["name"], "x-huza-placeholder": true}')
