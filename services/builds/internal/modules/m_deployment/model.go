package m_deployment

const Name = "deployment"
const Schema = "{\"type\": \"object\", \"properties\": {\"name\": {\"type\": \"string\"}, \"serviceName\": {\"type\": \"string\", \"default\": \"builds\", \"example\": \"builds\", \"description\": \"Editable initial-generation metadata; remove when no longer needed.\"}, \"modelName\": {\"type\": \"string\", \"default\": \"deployment\", \"example\": \"deployment\", \"description\": \"Editable initial-generation metadata; remove when no longer needed.\"}}, \"required\": [\"name\"], \"x-huza-placeholder\": true}"
