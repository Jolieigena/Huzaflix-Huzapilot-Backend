"""Optional response cache. Authorization stays in the existing HTTP middleware.

Epoch changes fence late fills. Redis is not transactional with the database;
a missed invalidation during an outage is bounded by the original entry TTL.
"""
from app.shared.monitoring import event
import hashlib
import json
import os
import time
import uuid
import redis

READ = "local version=redis.call('GET',KEYS[1]); if not version then version=ARGV[1]; redis.call('SET',KEYS[1],version) end; local value=redis.call('GET',KEYS[2]); if value and string.sub(value,1,string.len(version)+1)==version..'\\n' then return {version,string.sub(value,string.len(version)+2)} end; return {version,false}"
FILL = "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('SET',KEYS[2],ARGV[1]..'\\n'..ARGV[2],'PX',ARGV[3]) end; return false"

def bounded(name, default, minimum, maximum):
    try: return min(maximum,max(minimum,int(os.getenv(name,str(default)))))
    except ValueError: return default

class ResponseCache:
    def __init__(self):
        self.client = None
        self.ttl = bounded('CACHE_TTL_SECONDS',30,1,300)*1000
        self.max_bytes = bounded('CACHE_MAX_BYTES',1048576,1024,8388608)
        self.namespace = os.getenv('CACHE_NAMESPACE','huzapilot')
        self.service = os.getenv('SERVICE_NAME','service')
        url = os.getenv('REDIS_URL')
        if url and os.getenv('CACHE_ENABLED','true').lower()=='true':
            self.client=redis.Redis.from_url(url,decode_responses=True,socket_timeout=.2,socket_connect_timeout=.2,max_connections=8,retry_on_timeout=False)

    def prefix(self,model):
        digest=hashlib.sha256(json.dumps([self.namespace,self.service,model]).encode()).hexdigest()
        return 'huza:cache:{'+digest+'}'

    def invalidate(self,model):
        """Call after successful native writes too; failures never undo DB commits."""
        if self.client:
            try:
                self.client.set(self.prefix(model)+':version',uuid.uuid4().hex)
                event('cache','invalidate','success',model)
            except redis.RedisError: event('cache','invalidate','error',model)

    def read(self,model,fingerprint,load,bypass=False):
        if not self.client or bypass: return load(),'BYPASS'
        started=time.monotonic()
        prefix=self.prefix(model)
        keys=[prefix+':version',prefix+':entry:'+hashlib.sha256(fingerprint.encode()).hexdigest()]
        try:
            version,payload=self.client.eval(READ,2,*keys,uuid.uuid4().hex)
            if payload is not None:
                try: return json.loads(payload),'HIT'
                except (ValueError,TypeError): pass
        except redis.RedisError:
            event('cache','read-error','error',model)
            return load(),'BYPASS'
        result=load()  # Application errors must propagate, never cached or retried.
        ttl=self.ttl-int((time.monotonic()-started)*1000)
        try:
            payload=json.dumps(result,allow_nan=False,separators=(',',':'))
            if ttl>0 and len(payload.encode())<=self.max_bytes:
                self.client.eval(FILL,2,*keys,version,payload,ttl)
        except (redis.RedisError,TypeError,ValueError): event('cache','fill','error',model)
        return result,'MISS'

    def close(self):
        if self.client: self.client.close()

def cached(service,request,response,body,load):
    headers=['authorization','x-api-key','accept-language','origin']+os.getenv('CACHE_VARY_HEADERS','').lower().split(',')
    headers=list(dict.fromkeys(h.strip() for h in headers if h.strip()))
    fingerprint=json.dumps([request.method,request.url.path,request.url.query,[(h,request.headers.get(h,'')) for h in headers if h],body],separators=(',',':'))
    bypass=any(v in request.headers.get('cache-control','').lower() for v in ('no-cache','no-store')) or request.headers.get('x-cache-bypass')=='1' or request.query_params.get('cache')=='miss'
    value,status=service.store.cache.read(service.name,fingerprint,load,bypass)
    response.headers['X-Cache']=status
    response.headers['Cache-Control']='private, no-store'
    return value
