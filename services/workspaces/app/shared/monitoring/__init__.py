"""Common metric contract; labels never contain secrets or arbitrary paths."""
import functools
import json
import os
import re
import time
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
ENABLED=os.getenv('MONITORING_ENABLED')!='false'
REQUESTS=Counter('http_requests_total','Completed application requests',['method','route','status_code'])
DURATION=Histogram('http_request_duration_seconds','End to end HTTP duration',['method','route','status_code'],buckets=(.005,.025,.1,.25,.5,1,2.5,5,10,30))
ACTIVE=Gauge('http_requests_in_flight','Active application requests')
OPERATIONS=Histogram('huza_db_operation_duration_seconds','Shared store calls, including nested calls',['model','operation','outcome'],buckets=(.001,.005,.025,.1,.5,1,5,30))
EVENTS=Counter('huza_events_total','Shared infrastructure outcomes',['component','operation','outcome','model'])
try:
    with open('telemetry-routes.json') as f: ROUTES=json.load(f)
except FileNotFoundError: ROUTES=[]
PATTERNS=[(route,re.compile('^'+'/'.join('[^/]+' if part.startswith('{') else re.escape(part) for part in route.split('/'))+'/?$')) for route in ROUTES]
def route_name(path): return next((route for route,pattern in PATTERNS if pattern.fullmatch(path)),'unmatched')
def begin(path):
    tracked=ENABLED and path not in ('/health','/metrics','/actuator/prometheus')
    if tracked: ACTIVE.inc()
    return tracked
def finish(tracked,method,path,status,seconds,cache=None):
    if not tracked: return
    ACTIVE.dec(); method=method if method in ('GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS') else 'OTHER'
    route=route_name(path)
    REQUESTS.labels(method,route,str(status)).inc();DURATION.labels(method,route,str(status)).observe(seconds)
    if cache in ('HIT','MISS','BYPASS'): event('cache','read',cache)
def event(component,operation,outcome,model=''):
    if ENABLED: EVENTS.labels(component,operation,outcome,model).inc()
def observe(operation):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(self,module,*args,**kwargs):
            if not ENABLED: return fn(self,module,*args,**kwargs)
            start=time.monotonic();outcome='success'
            try: return fn(self,module,*args,**kwargs)
            except Exception: outcome='error';raise
            finally: OPERATIONS.labels(module,operation,outcome).observe(time.monotonic()-start)
        return wrapped
    return decorate

if os.getenv('DB_TYPE')=='sqlite':
    sqlite_size=Gauge('huza_sqlite_file_bytes','SQLite data and WAL file sizes',['file'])
    for filename in ('app.sqlite','app.sqlite-wal'):
        sqlite_size.labels(filename).set_function(lambda name=filename: os.path.getsize('/data/'+name) if os.path.exists('/data/'+name) else 0)
