import json
import os
import hmac
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, Response, HTMLResponse
from starlette.concurrency import run_in_threadpool
from app.shared.db.store import Store
from app.shared.security.api_keys import ApiKeys, KeyErrorResponse, required_scope
cfg=json.load(open('component.json'))
store=Store(cfg['modules'] if cfg['role']=='service' else ['api_keys'])
keys=ApiKeys(store,cfg.get('scopes',[])) if cfg['role']=='gateway' else None
app=FastAPI(openapi_url=None,docs_url=None,redoc_url=None)
counts={}
from app.shared.monitoring import generate_latest, CONTENT_TYPE_LATEST
def urls(s):
    key=s.upper().replace('-','_')
    return os.getenv(key+'_MICROSERVICE_URLS',os.getenv(key+'_MICROSERVICE_URL','')).split(',')
def identity_path(path):
    return path in ('/auth/login','/auth/refresh','/auth/logout','/auth/me','/auth/roles','/auth/accounts') or path.startswith(('/auth/roles/','/auth/accounts/'))
async def identity_forward(request):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            headers={k:v for k,v in request.headers.items() if k.lower() in ('authorization','content-type','cookie','origin','x-api-key')}
            headers.update({'X-Huza-Internal-Token':os.environ['API_KEY_AUTH_SECRET'],'X-Huza-Peer':request.client.host})
            response=await client.request(request.method,os.environ['IDENTITY_URL']+request.url.path,content=await request.body(),headers=headers)
        return Response(response.content,status_code=response.status_code,headers={k:v for k,v in response.headers.items() if k in ('content-type','set-cookie','cache-control','pragma')})
    except Exception:return JSONResponse({'error':'Identity authority unavailable'},status_code=503)
@app.middleware('http')
async def auth(request,call_next):
    if keys and identity_path(request.url.path):return await identity_forward(request)
    public=['/health','/openapi.json','/metrics'] + (['/auth/login','/auth/refresh','/api/docs.json','/api/docs'] if cfg['role']=='gateway' else [])
    if request.url.path == '/internal/api-keys/authorize' and keys:
        if not os.getenv('API_KEY_AUTH_SECRET') or not hmac.compare_digest(request.headers.get('x-huza-internal-token',''),os.environ['API_KEY_AUTH_SECRET']): return JSONResponse({'error':'Unauthorized'},status_code=401)
    elif request.url.path not in public:
        header=request.headers.get('authorization','')
        key=request.headers.get('x-api-key')
        if key is not None:
            if header: return JSONResponse({'error':'Ambiguous credentials'},status_code=401)
            scope=required_scope(cfg,request.url.path,request.method)
            try:
                if keys: status=await run_in_threadpool(keys.authorize,key,scope)
                else:
                    async with httpx.AsyncClient(timeout=5) as client:
                        response=await client.post(os.environ['AUTHORITY_URL']+'/internal/api-keys/authorize',json={'key':key,'scope':scope},headers={'X-Huza-Internal-Token':os.environ['API_KEY_AUTH_SECRET']})
                        status=response.status_code
            except Exception: status=503
            if status!=200: return JSONResponse({'error':'Insufficient scope' if status==403 else 'Invalid API key' if status==401 else 'Key authority unavailable'},status_code=status if status in (401,403) else 503)
            request.state.principal='api-key'
        else:
            if not header.startswith('Bearer '):return JSONResponse({'error':'Unauthorized'},status_code=401)
            scope='@admin' if keys and request.url.path.startswith('/auth/') else required_scope(cfg,request.url.path,request.method)
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    response=await client.post(os.environ['IDENTITY_URL']+'/authorize',json={'token':header[7:],'scope':scope},headers={'X-Huza-Internal-Token':os.environ['API_KEY_AUTH_SECRET']})
                if response.status_code!=200:return JSONResponse({'error':'Access denied'},status_code=response.status_code if response.status_code in (401,403) else 503)
                request.state.principal='admin' if response.json().get('admin') else 'user'
            except Exception:return JSONResponse({'error':'Identity authority unavailable'},status_code=503)
        if request.url.path.startswith('/auth/') and request.state.principal!='admin': return JSONResponse({'error':'Administrator access required'},status_code=403)
    response=await call_next(request)
    if 'x-instance-id' not in response.headers: response.headers['X-Instance-ID']=os.getenv('HOSTNAME',cfg['name'])
    return response
@app.get('/metrics')
def metrics(request:Request):
    import hmac
    token=os.getenv('MONITORING_TOKEN','')
    if token and not hmac.compare_digest(request.headers.get('authorization',''),'Bearer '+token): return Response(status_code=401)
    return Response(generate_latest(),headers={'Content-Type':CONTENT_TYPE_LATEST})
@app.get('/health')
def health(): return {'status':'ok','service':cfg['name']}
@app.get('/openapi.json')
def spec(): return cfg['spec']
if cfg['role']=='gateway':
    @app.post('/internal/api-keys/authorize')
    def authorize_key(body:dict):
        try:
            status=keys.authorize(body.get('key',''),body.get('scope',''))
            return JSONResponse({'allowed':status==200},status_code=status)
        except Exception: return JSONResponse({'error':'Key registry unavailable'},status_code=503)
    @app.get('/auth/scopes')
    def scopes(): return cfg['scopes']
    @app.api_route('/auth/api-keys',methods=['GET','POST'])
    @app.api_route('/auth/api-keys/{id}',methods=['PUT','DELETE'])
    def manage_keys(request:Request,body:dict=None,id:str=None):
        try:
            status,result=keys.manage(request.method,id,body)
            return JSONResponse(result,status_code=status,headers={'Cache-Control':'no-store'})
        except KeyErrorResponse as e: return JSONResponse({'error':e.message},status_code=e.status)
        except Exception: return JSONResponse({'error':'Key registry unavailable'},status_code=503)
    @app.get('/api/docs.json')
    async def docs():
        merged=json.loads(json.dumps(cfg['authSpec']))
        async with httpx.AsyncClient(timeout=10) as client:
            for s in cfg['services']:
                response=await client.get(urls(s)[0]+'/openapi.json')
                response.raise_for_status()
                spec=json.loads(json.dumps(response.json()).replace('#/components/schemas/','#/components/schemas/'+s+'__'))
                for p,ops in spec['paths'].items():
                    for op in ops.values(): op['operationId']=s+'__'+op['operationId']
                    merged['paths']['/api/'+s+p]=ops
                merged['components']['schemas'].update({s+'__'+k:v for k,v in spec['components']['schemas'].items()})
                merged['tags'].append({'name':s})
        return merged
    @app.get('/api/docs',response_class=HTMLResponse)
    def ui(): return '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>API documentation</title><link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css"></head><body><div id="swagger-ui"></div><script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script><script>SwaggerUIBundle({url:"/api/docs.json",dom_id:"#swagger-ui"})</script></body></html>'
    from app.routers.registry import register
    register(app)
else:
    from app.modules.registry import register
    app.state.models = register(app, store)

from app.shared.db.query import QueryError

@app.exception_handler(QueryError)
async def query_error(request, error):
    return JSONResponse({'error': str(error), 'code': error.code}, status_code=error.status)

@app.on_event('shutdown')
def close_resources():
    store.close()

from app.shared.middleware.context import RequestContextMiddleware
app.add_middleware(RequestContextMiddleware)
