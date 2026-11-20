package app

import (
	"huzapilot/component/internal/modules"
	"huzapilot/component/internal/routers"
	"huzapilot/component/internal/shared/config"
	"huzapilot/component/internal/shared/db"
	"huzapilot/component/internal/shared/middleware"
	"huzapilot/component/internal/shared/security"

	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

type Config = config.Config
type Doc = db.Doc

var cfg Config
var client = &http.Client{Timeout: 30 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}

func urls(s string) []string {
	k := strings.ToUpper(strings.ReplaceAll(s, "-", "_"))
	v := os.Getenv(k + "_MICROSERVICE_URLS")
	if v == "" {
		v = os.Getenv(k + "_MICROSERVICE_URL")
	}
	return strings.Split(v, ",")
}
func fail(c *gin.Context, err error) bool {
	if err != nil {
		log.Print(err)
		c.JSON(500, gin.H{"error": "Request failed"})
		return true
	}
	return false
}
func Run() {
	raw, err := os.ReadFile("component.json")
	if err != nil {
		log.Fatal(err)
	}
	if err = json.Unmarshal(raw, &cfg); err != nil {
		log.Fatal(err)
	}
	modelNames := cfg.Modules
	if cfg.Role == "gateway" {
		modelNames = []string{"api_keys"}
	}
	store, err := db.NewStore(modelNames)
	if err != nil {
		log.Fatal(err)
	}
	var keys *security.ApiKeys
	if cfg.Role == "gateway" {
		keys = &security.ApiKeys{Store: store, Scopes: cfg.Scopes}
	}
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(middleware.Context())
	r.Use(gin.Recovery())
	r.Use(func(c *gin.Context) { c.Header("X-Instance-ID", os.Getenv("HOSTNAME")); c.Next() })
	r.GET("/metrics", func(c *gin.Context) {
		token := os.Getenv("MONITORING_TOKEN")
		if token != "" && subtle.ConstantTimeCompare([]byte(c.GetHeader("Authorization")), []byte("Bearer "+token)) != 1 {
			c.AbortWithStatus(401)
			return
		}
		promhttp.Handler().ServeHTTP(c.Writer, c.Request)
	})
	r.GET("/health", func(c *gin.Context) { c.JSON(200, gin.H{"status": "ok", "service": cfg.Name}) })
	r.GET("/openapi.json", func(c *gin.Context) { c.JSON(200, cfg.Spec) })
	if cfg.Role == "gateway" {
		r.POST("/internal/api-keys/authorize", func(c *gin.Context) {
			secret := os.Getenv("API_KEY_AUTH_SECRET")
			if secret == "" || subtle.ConstantTimeCompare([]byte(c.GetHeader("X-Huza-Internal-Token")), []byte(secret)) != 1 {
				c.AbortWithStatus(401)
				return
			}
			var body map[string]string
			if c.ShouldBindJSON(&body) != nil {
				c.AbortWithStatus(400)
				return
			}
			status := keys.Authorize(body["key"], body["scope"])
			c.JSON(status, gin.H{"allowed": status == 200})
		})
		for _, path := range []string{"/auth/login", "/auth/refresh", "/auth/logout"} {
			r.POST(path, security.IdentityForward)
		}
		r.GET("/auth/me", security.IdentityForward)
		for _, kind := range []string{"roles", "accounts"} {
			r.GET("/auth/"+kind, security.IdentityForward)
			r.POST("/auth/"+kind, security.IdentityForward)
			r.PUT("/auth/"+kind+"/:id", security.IdentityForward)
			r.DELETE("/auth/"+kind+"/:id", security.IdentityForward)
		}
		r.GET("/api/docs.json", func(c *gin.Context) {
			raw, _ := json.Marshal(cfg.AuthSpec)
			merged := map[string]interface{}{}
			_ = json.Unmarshal(raw, &merged)
			paths := merged["paths"].(map[string]interface{})
			schemas := merged["components"].(map[string]interface{})["schemas"].(map[string]interface{})
			tags := merged["tags"].([]interface{})
			for _, s := range cfg.Services {
				resp, err := client.Get(urls(s)[0] + "/openapi.json")
				if fail(c, err) {
					return
				}
				b, err := io.ReadAll(resp.Body)
				resp.Body.Close()
				if fail(c, err) {
					return
				}
				if resp.StatusCode != 200 {
					c.AbortWithStatus(502)
					return
				}
				var spec map[string]interface{}
				err = json.Unmarshal(bytes.ReplaceAll(b, []byte("#/components/schemas/"), []byte("#/components/schemas/"+s+"__")), &spec)
				if fail(c, err) {
					return
				}
				for p, ops := range spec["paths"].(map[string]interface{}) {
					for _, v := range ops.(map[string]interface{}) {
						op := v.(map[string]interface{})
						op["operationId"] = s + "__" + op["operationId"].(string)
					}
					paths["/api/"+s+p] = ops
				}
				for k, v := range spec["components"].(map[string]interface{})["schemas"].(map[string]interface{}) {
					schemas[s+"__"+k] = v
				}
				tags = append(tags, gin.H{"name": s})
			}
			merged["tags"] = tags
			c.JSON(200, merged)
		})
		r.GET("/api/docs", func(c *gin.Context) {
			c.Data(200, "text/html", []byte(`<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>API documentation</title><link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css"></head><body><div id="swagger-ui"></div><script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script><script>SwaggerUIBundle({url:"/api/docs.json",dom_id:"#swagger-ui"})</script></body></html>`))
		})
	}
	r.Use(func(c *gin.Context) {
		h := c.GetHeader("Authorization")
		key, present := c.Request.Header["X-Api-Key"]
		if present {
			if h != "" || len(key) != 1 {
				c.AbortWithStatus(401)
				return
			}
			scope := security.RequiredScope(cfg, c.Request.URL.Path, c.Request.Method)
			status := 503
			if keys != nil {
				status = keys.Authorize(key[0], scope)
			} else {
				raw, _ := json.Marshal(Doc{"key": key[0], "scope": scope})
				req, err := http.NewRequestWithContext(c.Request.Context(), "POST", os.Getenv("AUTHORITY_URL")+"/internal/api-keys/authorize", bytes.NewReader(raw))
				if err == nil {
					req.Header.Set("Content-Type", "application/json")
					req.Header.Set("X-Huza-Internal-Token", os.Getenv("API_KEY_AUTH_SECRET"))
					resp, e := (&http.Client{Timeout: 5 * time.Second}).Do(req)
					if e == nil {
						status = resp.StatusCode
						resp.Body.Close()
					}
				}
			}
			if status != 200 {
				if status != 401 && status != 403 {
					status = 503
				}
				c.AbortWithStatusJSON(status, gin.H{"error": "API key authorization failed"})
				return
			}
			c.Set("principal", "api-key")
		} else {
			if !strings.HasPrefix(h, "Bearer ") {
				c.AbortWithStatus(401)
				return
			}
			scope := security.RequiredScope(cfg, c.Request.URL.Path, c.Request.Method)
			if keys != nil && strings.HasPrefix(c.Request.URL.Path, "/auth/") {
				scope = "@admin"
			}
			status, admin := security.IdentityAuthorize(c, h[7:], scope)
			if status != 200 {
				c.AbortWithStatus(status)
				return
			}
			if admin {
				c.Set("principal", "admin")
			} else {
				c.Set("principal", "user")
			}
		}

		if strings.HasPrefix(c.Request.URL.Path, "/auth/") && c.GetString("principal") != "admin" {
			c.AbortWithStatus(403)
			return
		}
		c.Next()
	})
	if keys != nil {
		r.GET("/auth/scopes", func(c *gin.Context) { c.JSON(200, cfg.Scopes) })
		management := func(c *gin.Context) {
			body := Doc{}
			if (c.Request.Method == "POST" || c.Request.Method == "PUT") && c.ShouldBindJSON(&body) != nil {
				c.AbortWithStatus(400)
				return
			}
			status, result := keys.Manage(c.Request.Method, c.Param("id"), body)
			c.Header("Cache-Control", "no-store")
			c.JSON(status, result)
		}
		r.GET("/auth/api-keys", management)
		r.POST("/auth/api-keys", management)
		r.PUT("/auth/api-keys/:id", management)
		r.DELETE("/auth/api-keys/:id", management)
	}
	if cfg.Role == "gateway" {
		routers.Register(r)
	} else {
		if err := modules.Register(r, store); err != nil {
			log.Fatal(err)
		}
	}
	defer store.Close()

	port := os.Getenv("PORT")
	if port == "" {
		port = "8000"
	}
	server := &http.Server{Addr: fmt.Sprintf(":%s", port), Handler: r, ReadHeaderTimeout: 10 * time.Second}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	failures := make(chan error, 1)
	go func() { failures <- server.ListenAndServe() }()
	select {
	case <-ctx.Done():
	case err := <-failures:
		if !errors.Is(err, http.ErrServerClosed) {
			log.Print(err)
		}
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := server.Shutdown(shutdown); err != nil {
		log.Print(err)
		_ = server.Close()
	}
}
