#!/usr/bin/env python3
"""Manifest-driven checks. Failures are recorded, never converted into skipped passes."""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

root=Path(sys.argv[1]).resolve()
manifest=json.loads((root/'.huzapilot.json').read_text())
env=dict(os.environ)
for line in (root/'.env').read_text().splitlines():
    if '=' in line and not line.startswith('#'):
        key,value=line.split('=',1);env.setdefault(key,value)
base='http://127.0.0.1:'+env.get('GATEWAY_PORT','8080')
frontend='http://127.0.0.1:'+env.get('FRONTEND_PORT','5173')
compose=['docker','compose'] if subprocess.run(['docker','compose','version'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0 else ['docker-compose']
results=[]

def check(label,fn):
    try:
        fn();results.append({'check':label,'pass':True});print('PASS',label,flush=True)
    except Exception as e:
        results.append({'check':label,'pass':False,'rootCause':str(e)});print('FAIL',label,str(e),flush=True)

def assert_(test,message):
    if not test:raise AssertionError(message)

def cmd(args,**kwargs):
    result=subprocess.run(args,cwd=root,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,**kwargs)
    if result.returncode:raise RuntimeError(result.stdout[-6000:])
    return result.stdout

def request(path,method='GET',data=None,token=None,api_key=None,origin=None,extra_headers=None):
    headers={'Content-Type':'application/json'}
    if token:headers['Authorization']='Bearer '+token
    if api_key is not None:headers['X-API-Key']=api_key
    headers.update(extra_headers or {})
    req=urllib.request.Request((origin or base)+path,data=json.dumps(data).encode() if data is not None else None,headers=headers,method=method)
    try:response=urllib.request.urlopen(req,timeout=40)
    except urllib.error.HTTPError as e:response=e
    raw=response.read()
    try:body=json.loads(raw) if raw else None
    except ValueError:body=raw.decode()
    return response.status,body,response.headers

def health():
    deadline=time.time()+360
    while time.time()<deadline:
        try:
            if request('/health')[0]==200:return
        except OSError:pass
        time.sleep(3)
    raise RuntimeError('Gateway did not become ready in 360 seconds')
check('gateway health',health)
for svc in manifest['services']:
    for container in svc['containers']:
        check(f"{svc['name']} {svc['lang']}×{svc['database']} health {container}",lambda c=container:assert_('ok' in cmd(compose+['exec','-T',c,'curl','-fsS','http://localhost:8000/health']),'Health response missing status'))

def swagger():
    status,spec,_=request('/api/docs.json');assert_(status==200,'Swagger unavailable')
    assert_(spec.get('openapi','').startswith('3.'),'Missing OpenAPI version')
    for svc in manifest['services']:
        assert_(any(t['name']==svc['name'] for t in spec['tags']),'Missing service '+svc['name'])
        for route in svc['routes']:
            assert_(route['list'] in spec['paths'] and route['detail'] in spec['paths'],'Missing CRUD routes')
    def refs(node):
        if isinstance(node,dict):
            if '$ref' in node:
                ref=node['$ref'];target=spec
                for key in ref.removeprefix('#/').split('/'):target=target[key]
            for value in node.values():refs(value)
        elif isinstance(node,list):
            for value in node:refs(value)
    refs(spec)
    (root/'merged-smoke-openapi.json').write_text(json.dumps(spec))
check('merged Swagger and references',swagger)
tokens={}
def login_check():
    global tokens
    status,body,_=request('/auth/login','POST',{'username':env['AUTH_USERNAME'],'password':env['AUTH_PASSWORD']})
    assert_(status==200 and isinstance(body,dict) and 'access_token' in body,'Login failed')
    tokens=body
check('JWT login',login_check)
token=tokens.get('access_token','') if isinstance(tokens,dict) else ''
check('invalid login rejected',lambda:assert_(request('/auth/login','POST',{'username':env['AUTH_USERNAME'],'password':'incorrect'})[0]==401,'Invalid password accepted'))
def refresh_check():
    global tokens,token
    status,body,_=request('/auth/refresh','POST',{'refresh_token':tokens.get('refresh_token','')});assert_(status==200,'Refresh failed');tokens=body;token=body['access_token']
check('JWT refresh',refresh_check)

def sample(schema,marker):
    result={}
    for name,field in schema.get('properties',{}).items():
        kind=field.get('type','string')
        result[name]=field.get('enum',[None])[0] if field.get('enum') else {'string':marker,'integer':7,'number':7.5,'boolean':True,'object':{},'array':[]}.get(kind,marker)
        if field.get('format')=='email':result[name]=marker+'@example.com'
        if field.get('format')=='date':result[name]='2026-01-01'
    return result

def verify_persistence(svc,route,id,data):
    table=route['table'];container=svc['databaseContainer'];db=svc['database'];dbname=svc['databaseName']
    sql=f"SELECT data FROM {table} WHERE id = '{id}'"
    if db=='postgres':args=['sh','-c','PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1"','sh',sql]
    elif db=='mysql':args=['sh','-c','MYSQL_PWD="$MYSQL_PASSWORD" mysql -u "$MYSQL_USER" -D "$MYSQL_DATABASE" -N -e "$1"','sh',sql]
    elif db=='mongodb':args=['sh','-c','mongosh --quiet -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --eval "$1"','sh',f'JSON.stringify(db.getSiblingDB("{dbname}").getCollection("{table}").findOne({{_id:"{id}"}}))']
    elif db=='cassandra':args=['sh','-c','cqlsh -u cassandra -p "$CASSANDRA_PASSWORD" -e "$1"','sh',f"SELECT data FROM {dbname}.{table} WHERE id = '{id}'"]
    else:args=['sqlite3','/data/app.sqlite',sql]
    output=cmd(compose+['exec','-T',container]+args)
    assert_(bool(output.strip()) and all(str(v).lower() in output.lower() for v in data.values() if isinstance(v,(str,int,float))),'Direct database query did not find payload: '+output)

browser_seeds=[]
for svc in manifest['services']:
    route=svc['routes'][0];path=route['list'];label=f"{svc['name']} {svc['lang']}×{svc['database']}"
    check(label+' unauthenticated 401',lambda p=path:assert_(request(p)[0]==401,'Missing-token request accepted'))
    check(label+' forged JWT 401',lambda p=path:assert_(request(p,token='invalid')[0]==401,'Invalid token accepted'))
    check(label+' refresh token cannot access data',lambda p=path:assert_(request(p,token=tokens.get('refresh_token',''))[0]==401,'Refresh token accepted as access token'))
    for route in svc['routes']:
        path=route['list']
        def crud(svc=svc,route=route,path=path):
            marker='smoke-'+str(time.time_ns());data=sample(route['schema'],marker)
            status,record,_=request(path,'POST',data,token);assert_(status==201,f'Create: {status} {record}');id=record['id'];item=path+id
            status,rows,_=request(path,token=token);assert_(status==200 and any(r['id']==id for r in rows),'Created record absent from list')
            assert_(request(item,token=token)[1]['id']==id,'Detail does not return created ID')
            verify_persistence(svc,route,id,data)
            changed=sample(route['schema'],marker+'-updated')
            status,_,_=request(item,'PUT',changed,token);assert_(status==200,'Update failed')
            read=request(item,token=token)[1];assert_(all(read[k]==v for k,v in changed.items()),'Update did not persist')
            assert_(request(item,'DELETE',token=token)[0]==200,'Delete failed');assert_(request(item,token=token)[0]==404,'Deleted record still readable')
            seed=sample(route['schema'],marker+'-browser');status,record,_=request(path,'POST',seed,token);assert_(status==201,'Browser seed failed');browser_seeds.append({'service':svc['name'],'module':route['module'],'data':seed,'id':record['id']})
        check(label+' / '+route['module']+' CRUD and direct persistence',crud)
    def balancing(svc=svc,path=path):
        seen=set()
        for _ in range(svc['instances']*3):
            status,_,headers=request(path,token=token);assert_(status==200,'Load-balancing request failed');seen.add(headers.get('X-Instance-ID'))
        expected={cmd(compose+['exec','-T',c,'cat','/etc/hostname']).strip() for c in svc['containers']}
        assert_(seen==expected,f'Expected upstream instances {expected}, observed {seen}')
    check(label+' load balancing',balancing)

from smoke_queries import run_checks as query_checks
query_checks(manifest,request,check,assert_,token,sample,cmd,compose)

from smoke_cache import run_checks as cache_checks
cache_checks(manifest,request,check,assert_,token,sample,cmd,compose)

from smoke_messaging import run_checks as messaging_checks
messaging_checks(manifest,check,assert_,cmd,compose)

from smoke_api_keys import run_checks
run_checks(manifest,env,request,check,assert_,cmd,compose,token,sample,health,verify_persistence)

from smoke_roles import run_checks as role_checks
role_checks(manifest,env,request,check,assert_,cmd,compose,token,sample,health,verify_persistence)

def pages_css():
    pages=json.loads((root/'client-portal/pages.json').read_text())
    templates_enabled=manifest['frontend'].get('componentLibrary',{}).get('enabled',True)
    expected=4+int(templates_enabled)+sum(4*len(s['modules']) for s in manifest['services'])
    assert_(len(pages)==expected==manifest['frontend']['pageCount'],'Page estimate mismatch')
    for page in pages:
        if 'file' in page:assert_((root/'client-portal'/page['file']).is_file(),'Missing generated page')
    for source in (root/'client-portal/src').rglob('*.tsx'):
        css=source.with_suffix('.css');assert_(css.exists(),f'Missing CSS for {source}')
        for classes in re.findall(r'className="([^"]+)"',source.read_text()):
            for cls in classes.split():assert_(re.search(r'\.'+re.escape(cls)+r'(?![a-zA-Z0-9_-])',css.read_text()),f'Undefined class {cls} in {source}')
        assert_('style={' not in source.read_text(),'Inline visual style')
check('frontend page estimate and CSS coverage',pages_css)
check('frontend production build',lambda:cmd(['docker','build','--target','build','-t',manifest['project']+'-frontend-check','client-portal'],timeout=360))
(root/'browser-seeds.json').write_text(json.dumps(browser_seeds))
def browser():
    cmd(['docker','build','-t','huza-smoke-browser','-f','client-portal/Dockerfile.smoke','client-portal'],timeout=600)
    cmd(['docker','run','--rm','--network','host','--ipc','host','-e','AUTH_USERNAME','-e','AUTH_PASSWORD','-e','GATEWAY_PORT','-e','FRONTEND_PORT','-e','PROJECT_ROOT=/project','-v',str(root)+':/project:ro','huza-smoke-browser'],timeout=240)
check('browser Swagger styling, frontend login, real lists, create modal',browser)
from smoke_monitoring import run_checks as monitoring_checks
monitoring_checks(manifest,check,cmd,compose,root)
def components():
    component_results=root/'component-results'
    component_results.mkdir(exist_ok=True)
    container=manifest['project']+'-component-check'
    try:
        with (component_results/'browser.log').open('w') as log:
            result=subprocess.run(['docker','run','--rm','--name',container,'--network','host','--ipc','host','-e','COMPONENT_BASE_URL=http://127.0.0.1:'+env.get('FRONTEND_PORT','5173'),'-e','COMPONENT_REPORT_DIR=/component-results','-e','PROJECT_ROOT=/project','-v',str(root)+':/project:ro','-v',str(component_results)+':/component-results','huza-smoke-browser','node','component-test.mjs'],cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=600)
        assert_(result.returncode==0,'Component audit failed; see component-results/browser.log and results.json')
    finally:
        # Killing a timed-out docker CLI does not guarantee its container stops.
        subprocess.run(['docker','rm','-f',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
if manifest['frontend'].get('componentLibrary',{}).get('enabled',True):
    check('frontend component studio accessibility, palettes and interactions',components)
else:
    def without_templates():
        portal=root/'client-portal'
        for path in ('src/library','src/studio','component-forge','src/generated-components.ts'):
            assert_(not (portal/path).exists(),f'Excluded template asset still present: {path}')
        assert_(not any(p['path']=='/components' for p in manifest['frontend']['pages']), 'Gallery route still registered')
    check('frontend no-template excludes gallery and review assets',without_templates)
report={'passed':sum(r['pass'] for r in results),'total':len(results),'checks':results}
report['passRate']=report['passed']/report['total']
(root/'smoke-results.json').write_text(json.dumps(report,indent=2))
print(json.dumps({k:v for k,v in report.items() if k!='checks'}))
sys.exit(0 if all(r['pass'] for r in results) else 1)
