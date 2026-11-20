export const schema = {
  "type": "object",
  "properties": {
    "name": {
      "type": "string"
    },
    "serviceName": {
      "type": "string",
      "default": "projects",
      "example": "projects",
      "description": "Editable initial-generation metadata; remove when no longer needed."
    },
    "modelName": {
      "type": "string",
      "default": "template",
      "example": "template",
      "description": "Editable initial-generation metadata; remove when no longer needed."
    }
  },
  "required": [
    "name"
  ],
  "x-huza-placeholder": true
};
