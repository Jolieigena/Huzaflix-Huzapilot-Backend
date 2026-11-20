package config

type Config struct {
 Name string
 Role string
 Modules []string
 Services []string
 Spec map[string]interface{}
 Scopes []string
 AuthSpec map[string]interface{}
}
