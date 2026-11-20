package http

import (
	"github.com/gin-gonic/gin"
	"io"
	"net/http"
	"os"
	"strings"
	"sync/atomic"
	"time"
)

func RegisterGateway(router *gin.Engine, service string) {
	key := strings.ToUpper(strings.ReplaceAll(service, "-", "_"))
	value := os.Getenv(key + "_MICROSERVICE_URLS")
	if value == "" {
		value = os.Getenv(key + "_MICROSERVICE_URL")
	}
	all := strings.Split(value, ",")
	var count atomic.Uint64
	client := &http.Client{Timeout: 30 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	router.Any("/api/"+service+"/*path", func(c *gin.Context) {
		target := all[(count.Add(1)-1)%uint64(len(all))] + c.Param("path")
		if c.Request.URL.RawQuery != "" {
			target += "?" + c.Request.URL.RawQuery
		}
		request, err := http.NewRequestWithContext(c.Request.Context(), c.Request.Method, target, c.Request.Body)
		if err != nil {
			respond(c, 500, nil, err)
			return
		}
		if auth := c.GetHeader("Authorization"); auth != "" {
			request.Header.Set("Authorization", auth)
		} else {
			request.Header.Set("X-API-Key", c.GetHeader("X-API-Key"))
		}
		request.Header.Set("Content-Type", "application/json")
		request.Header.Set("X-Request-ID", c.GetHeader("X-Request-ID"))
		for _, name := range strings.Split("Cache-Control,X-Cache-Bypass,Accept-Language,Origin,"+os.Getenv("CACHE_VARY_HEADERS"), ",") {
			name = strings.TrimSpace(name)
			if name != "" {
				if value := c.GetHeader(name); value != "" {
					request.Header.Set(name, value)
				}
			}
		}
		response, err := client.Do(request)
		if err != nil {
			respond(c, 502, nil, err)
			return
		}
		defer response.Body.Close()
		c.Header("Content-Type", response.Header.Get("Content-Type"))
		c.Header("X-Instance-ID", response.Header.Get("X-Instance-ID"))
		c.Header("X-Cache", response.Header.Get("X-Cache"))
		c.Header("Cache-Control", response.Header.Get("Cache-Control"))
		c.Status(response.StatusCode)
		_, _ = io.Copy(c.Writer, response.Body)
	})
}
