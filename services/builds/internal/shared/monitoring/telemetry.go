// Package monitoring implements the shared metric contract with bounded labels.
package monitoring

import (
	"encoding/json"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"os"
	"regexp"
	"strings"
	"time"
)

var Enabled = os.Getenv("MONITORING_ENABLED") != "false"
var requests = promauto.NewCounterVec(prometheus.CounterOpts{Name: "http_requests_total", Help: "Completed application requests"}, []string{"method", "route", "status_code"})
var duration = promauto.NewHistogramVec(prometheus.HistogramOpts{Name: "http_request_duration_seconds", Help: "End to end HTTP duration", Buckets: []float64{.005, .025, .1, .25, .5, 1, 2.5, 5, 10, 30}}, []string{"method", "route", "status_code"})
var active = promauto.NewGauge(prometheus.GaugeOpts{Name: "http_requests_in_flight", Help: "Active application requests"})
var operations = promauto.NewHistogramVec(prometheus.HistogramOpts{Name: "huza_db_operation_duration_seconds", Help: "Shared store calls, including nested calls", Buckets: []float64{.001, .005, .025, .1, .5, 1, 5, 30}}, []string{"model", "operation", "outcome"})
var events = promauto.NewCounterVec(prometheus.CounterOpts{Name: "huza_events_total", Help: "Shared infrastructure outcomes"}, []string{"component", "operation", "outcome", "model"})

type routePattern struct {
	name string
	re   *regexp.Regexp
}

var patterns = []routePattern{}

func init() {
	raw, err := os.ReadFile("telemetry-routes.json")
	if err != nil {
		return
	}
	var routes []string
	if json.Unmarshal(raw, &routes) != nil {
		return
	}
	for _, r := range routes {
		parts := strings.Split(r, "/")
		for i, p := range parts {
			if strings.HasPrefix(p, "{") {
				parts[i] = "[^/]+"
			} else {
				parts[i] = regexp.QuoteMeta(p)
			}
		}
		patterns = append(patterns, routePattern{r, regexp.MustCompile("^" + strings.Join(parts, "/") + "/?$")})
	}
}
func Route(path string) string {
	for _, p := range patterns {
		if p.re.MatchString(path) {
			return p.name
		}
	}
	return "unmatched"
}
func Begin(path string) bool {
	tracked := Enabled && path != "/health" && path != "/metrics" && path != "/actuator/prometheus"
	if tracked {
		active.Inc()
	}
	return tracked
}
func Finish(tracked bool, method, path, status string, seconds float64, cache string) {
	if !tracked {
		return
	}
	active.Dec()
	switch method {
	case "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS":
	default:
		method = "OTHER"
	}
	route := Route(path)
	requests.WithLabelValues(method, route, status).Inc()
	duration.WithLabelValues(method, route, status).Observe(seconds)
	switch cache {
	case "HIT", "MISS", "BYPASS":
		Event("cache", "read", cache, "")
	}
}
func Event(component, operation, outcome, model string) {
	if Enabled {
		events.WithLabelValues(component, operation, outcome, model).Inc()
	}
}
func Observe(model, operation string, start time.Time, err error) {
	if !Enabled {
		return
	}
	outcome := "success"
	if err != nil {
		outcome = "error"
	}
	operations.WithLabelValues(model, operation, outcome).Observe(time.Since(start).Seconds())
}

func init() {
	if os.Getenv("DB_TYPE") == "sqlite" {
		for _, file := range []string{"app.sqlite", "app.sqlite-wal"} {
			name := file
			promauto.NewGaugeFunc(prometheus.GaugeOpts{Name: "huza_sqlite_file_bytes", Help: "SQLite data and WAL file sizes", ConstLabels: prometheus.Labels{"file": name}}, func() float64 {
				info, err := os.Stat("/data/" + name)
				if err != nil {
					return 0
				}
				return float64(info.Size())
			})
		}
	}
}
