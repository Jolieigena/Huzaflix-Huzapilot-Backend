// Package cache provides optional, credential-partitioned HTTP response caching.
// Redis/DB writes are not transactional; missed invalidation is bounded by TTL.
package cache

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"huzapilot/component/internal/shared/monitoring"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

const readScript = "local version=redis.call('GET',KEYS[1]); if not version then version=ARGV[1]; redis.call('SET',KEYS[1],version) end; local value=redis.call('GET',KEYS[2]); if value and string.sub(value,1,string.len(version)+1)==version..'\\n' then return {version,string.sub(value,string.len(version)+2)} end; return {version,false}"
const fillScript = "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('SET',KEYS[2],ARGV[1]..'\\n'..ARGV[2],'PX',ARGV[3]) end; return false"

type Cache struct {
	client   *redis.Client
	ttl      time.Duration
	maxBytes int
}

func bounded(name string, fallback, min, max int) int {
	n, e := strconv.Atoi(os.Getenv(name))
	if e != nil {
		return fallback
	}
	if n < min {
		return min
	}
	if n > max {
		return max
	}
	return n
}
func New() *Cache {
	c := &Cache{ttl: time.Duration(bounded("CACHE_TTL_SECONDS", 30, 1, 300)) * time.Second, maxBytes: bounded("CACHE_MAX_BYTES", 1048576, 1024, 8388608)}
	if os.Getenv("REDIS_URL") == "" || os.Getenv("CACHE_ENABLED") == "false" {
		return c
	}
	options, e := redis.ParseURL(os.Getenv("REDIS_URL"))
	if e != nil {
		return c
	}
	options.DialTimeout = 200 * time.Millisecond
	options.ReadTimeout = 200 * time.Millisecond
	options.WriteTimeout = 200 * time.Millisecond
	options.PoolTimeout = 200 * time.Millisecond
	options.PoolSize = 8
	options.MaxRetries = -1
	options.ContextTimeoutEnabled = true
	c.client = redis.NewClient(options)
	return c
}
func hash(s string) string { sum := sha256.Sum256([]byte(s)); return hex.EncodeToString(sum[:]) }
func (c *Cache) prefix(model string) string {
	return "huza:cache:{" + hash(os.Getenv("CACHE_NAMESPACE")+"\n"+os.Getenv("SERVICE_NAME")+"\n"+model) + "}"
}
func (c *Cache) Invalidate(model string) {
	if c == nil || c.client == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	outcome := "success"
	if c.client.Set(ctx, c.prefix(model)+":version", uuid.NewString(), 0).Err() != nil {
		outcome = "error"
	}
	monitoring.Event("cache", "invalidate", outcome, model)
}
func (c *Cache) Read(model, fingerprint string, load func() (any, error), bypass bool) (any, string, error) {
	if c == nil || c.client == nil || bypass {
		value, e := load()
		return value, "BYPASS", e
	}
	start := time.Now()
	keys := []string{c.prefix(model) + ":version", c.prefix(model) + ":entry:" + hash(fingerprint)}
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	reply, e := c.client.Eval(ctx, readScript, keys, uuid.NewString()).Slice()
	cancel()
	if e != nil || len(reply) != 2 {
		monitoring.Event("cache", "read-error", "error", model)
		value, e := load()
		return value, "BYPASS", e
	}
	version, ok := reply[0].(string)
	if !ok {
		value, e := load()
		return value, "BYPASS", e
	}
	if raw, ok := reply[1].(string); ok {
		var value any
		if json.Unmarshal([]byte(raw), &value) == nil {
			return value, "HIT", nil
		}
	}
	value, e := load()
	if e != nil {
		return nil, "MISS", e
	}
	remaining := c.ttl - time.Since(start)
	if raw, e := json.Marshal(value); e == nil && len(raw) <= c.maxBytes && remaining.Milliseconds() > 0 {
		ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
		if c.client.Eval(ctx, fillScript, keys, version, string(raw), remaining.Milliseconds()).Err() != nil {
			monitoring.Event("cache", "fill", "error", model)
		}
		cancel()
	}
	return value, "MISS", nil
}
func (c *Cache) HTTP(model string, req *http.Request, body any, load func() (any, error)) (any, string, error) {
	headers := map[string]string{}
	for _, h := range strings.Split("authorization,x-api-key,accept-language,origin,"+strings.ToLower(os.Getenv("CACHE_VARY_HEADERS")), ",") {
		h = strings.TrimSpace(h)
		if h != "" {
			headers[h] = req.Header.Get(h)
		}
	}
	fingerprint, e := json.Marshal([]any{req.Method, req.URL.RequestURI(), headers, body})
	if e != nil {
		value, e := load()
		return value, "BYPASS", e
	}
	control := strings.ToLower(req.Header.Get("Cache-Control"))
	bypass := strings.Contains(control, "no-cache") || strings.Contains(control, "no-store") || req.Header.Get("X-Cache-Bypass") == "1" || req.URL.Query().Get("cache") == "miss"
	return c.Read(model, string(fingerprint), load, bypass)
}
func (c *Cache) Close() {
	if c != nil && c.client != nil {
		c.client.Close()
	}
}
