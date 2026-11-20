package middleware

import (
	"encoding/json"
	"fmt"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"huzapilot/component/internal/shared/monitoring"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"sync"
	"time"
)

var validID = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)

func Context() gin.HandlerFunc {
	type window struct {
		Count   int
		Expires time.Time
	}
	windows := map[string]window{}
	var mutex sync.Mutex
	limit, _ := strconv.Atoi(os.Getenv("RATE_LIMIT_PER_MINUTE"))
	bodyLimit, err := strconv.Atoi(os.Getenv("REQUEST_MAX_BYTES"))
	if err != nil || bodyLimit < 1024 {
		bodyLimit = 1048576
	}
	return func(c *gin.Context) {
		requestID := c.GetHeader("X-Request-ID")
		if !validID.MatchString(requestID) {
			requestID = uuid.NewString()
		}
		c.Request.Header.Set("X-Request-ID", requestID)
		c.Header("X-Request-ID", requestID)
		c.Set("requestId", requestID)
		started := time.Now()
		tracked := monitoring.Begin(c.Request.URL.Path)
		defer func() {
			monitoring.Finish(tracked, c.Request.Method, c.Request.URL.Path, strconv.Itoa(c.Writer.Status()), time.Since(started).Seconds(), c.Writer.Header().Get("X-Cache"))
			if os.Getenv("REQUEST_LOGGING") != "false" && c.Request.URL.Path != "/health" && c.Request.URL.Path != "/metrics" {
				data, _ := json.Marshal(map[string]any{"event": "http_request", "timestamp": time.Now().UTC().Format(time.RFC3339Nano), "level": "info", "service": os.Getenv("SERVICE_NAME"), "instance": os.Getenv("HOSTNAME"), "requestId": requestID, "method": c.Request.Method, "route": monitoring.Route(c.Request.URL.Path), "status": c.Writer.Status(), "durationMs": time.Since(started).Milliseconds()})
				fmt.Println(string(data))
			}
		}()
		if limit > 0 && c.Request.URL.Path != "/health" {
			key := c.RemoteIP()
			now := time.Now()
			mutex.Lock()
			w := windows[key]
			if w.Expires.Before(now) {
				w = window{Expires: now.Add(time.Minute)}
			}
			w.Count++
			windows[key] = w
			if len(windows) > 10000 {
				for k := range windows {
					if k != key {
						delete(windows, k)
						break
					}
				}
			}
			mutex.Unlock()
			if w.Count > limit {
				c.Header("Retry-After", strconv.Itoa(max(1, int(time.Until(w.Expires).Seconds()))))
				c.AbortWithStatusJSON(429, gin.H{"error": "Rate limit exceeded", "requestId": requestID})
				return
			}
		}
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, int64(bodyLimit))
		c.Next()
	}
}
