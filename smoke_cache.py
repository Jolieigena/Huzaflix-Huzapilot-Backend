"""Manifest-driven response-cache regression checks; no generated code edits."""
import os
import time
import uuid


def run_checks(manifest, request, check, assert_, token, sample, cmd, compose):
    enabled=manifest.get('cache',{}).get('enabled',False)
    expected='MISS' if enabled else 'BYPASS'
    def expect(result, status, label):
        assert_(result[0]==status,f'{label}: expected {status}, got {result[0]}')
        return result[1]
    def cache_status(result, status, label):
        expect(result,200,label)
        assert_(result[2].get('X-Cache')==status,f'{label}: expected cache {status}, got {result[2].get("X-Cache")}')
        return result[1]
    for svc in manifest['services']:
        for route in svc['routes']:
            def model(svc=svc,route=route):
                path=route['list']; probe='?probe='+uuid.uuid4().hex
                url=path+probe; item=None; key=None
                def read(url=url): return request(url,token=token)
                def warm(url=url):
                    cache_status(read(url),expected,'cold read')
                    cache_status(read(url),'HIT' if enabled else 'BYPASS','repeat read')
                try:
                    warm()
                    expect(request(url),401,'Cached route still needs authorization')
                    # Headers configured with whitespace/case must partition requests.
                    vary=[h.strip() for h in os.getenv('CACHE_VARY_HEADERS','').split(',') if h.strip() and h.strip().lower() not in ('authorization','x-api-key','origin','cookie')]
                    for header in ['Accept-Language',*vary]:
                        a={header:'context-a'};b={header:'context-b'}
                        cache_status(request(url,token=token,extra_headers=a),expected,'Context A miss: '+header)
                        cache_status(request(url,token=token,extra_headers=b),expected,'Context B miss: '+header)
                        cache_status(request(url,token=token,extra_headers=a),'HIT' if enabled else 'BYPASS','Context A repeat: '+header)

                    data=sample(route['schema'],'cache-'+uuid.uuid4().hex[:12])
                    row=expect(request(path,'POST',data,token),201,'Create');item=path+row['id']
                    rows=cache_status(read(),expected,'Create invalidates list')
                    assert_(any(v['id']==row['id'] for v in rows),'Created record absent')
                    cache_status(read(),'HIT' if enabled else 'BYPASS','List refilled')
                    warm(item)
                    # POST read endpoints cache by complete body, not by HTTP verb alone.
                    query=path+'_query'; body={'filter':{'id':row['id']},'options':{}}
                    cache_status(request(query,'POST',body,token),expected,'Query miss')
                    cache_status(request(query,'POST',body,token),'HIT' if enabled else 'BYPASS','Query hit')
                    other={'filter':{'id':'absent-'+uuid.uuid4().hex},'options':{}}
                    assert_(cache_status(request(query,'POST',other,token),expected,'Different query key')==[],'Query bodies collide')
                    aggregate=path+'_aggregate';pipeline={'pipeline':[{'$match':{'id':row['id']}},{'$count':'total'}]}
                    cache_status(request(aggregate,'POST',pipeline,token),expected,'Aggregate miss')
                    cache_status(request(aggregate,'POST',pipeline,token),'HIT' if enabled else 'BYPASS','Aggregate hit')
                    expect(request(query,'POST',{'filter':{'$bad':True}},token),400,'Malformed query rejected')
                    cache_status(read(),'HIT' if enabled else 'BYPASS','Read-only POST does not invalidate')
                    expect(request(path+'absent-'+uuid.uuid4().hex,'PUT',data,token),404,'Missing-record write rejected')
                    cache_status(read(),'HIT' if enabled else 'BYPASS','Failed write does not invalidate')
                    updated=sample(route['schema'],'updated-'+uuid.uuid4().hex[:12])
                    expect(request(item,'PUT',updated,token),200,'Update')
                    detail=cache_status(read(item),expected,'Update invalidates detail')
                    assert_(all(detail.get(k)==v for k,v in updated.items()),'Stale detail after update')
                    cache_status(read(),expected,'Update invalidates list')
                    cache_status(request(query,'POST',body,token),expected,'Update invalidates query')
                    cache_status(request(aggregate,'POST',pipeline,token),expected,'Update invalidates aggregate')
                    # Credentials are partitioned; a warm response cannot bypass live scope checks.
                    key=expect(request('/auth/api-keys','POST',{'name':'Cache regression','scopes':[svc['name']+':'+route['module']+':read']},token),201,'Create scoped key')
                    cache_status(request(url,api_key=key['key']),expected,'Credential partition')
                    cache_status(request(url,api_key=key['key']),'HIT' if enabled else 'BYPASS','Scoped key hit')
                    expect(request('/auth/api-keys/'+key['id'],'PUT',{'name':key['name'],'scopes':[],'expiresAt':None},token),200,'Reduce scopes')
                    expect(request(url,api_key=key['key']),403,'Warm cache respects reduced scopes')
                    cache_status(request(url,token=token,extra_headers={'X-Cache-Bypass':'1'}),'BYPASS','Explicit bypass reaches service')
                    cache_status(request(url,token=token,extra_headers={'Cache-Control':'no-cache'}),'BYPASS','Cache-Control reaches service')
                    expect(request(item,'DELETE',token=token),200,'Delete');item=None
                    assert_(cache_status(request(aggregate,'POST',pipeline,token),expected,'Delete invalidates aggregate')==[],'Stale aggregate after delete')
                    cache_status(read(),expected,'Delete invalidates list')
                    for _ in range(2): expect(request(path+row['id'],token=token),404,'Missing detail is not cached as success')
                finally:
                    if item: request(item,'DELETE',token=token)
                    if key: request('/auth/api-keys/'+key['id'],'DELETE',token=token)
            check(f"Redis cache {svc['lang']}×{svc['database']} {svc['name']}/{route['module']}: reads, mutations, credentials and live scopes",model)
    if not enabled:return
    def outage():
        targets=[]
        for svc in manifest['services']:
            route=svc['routes'][0];data=sample(route['schema'],'outage-'+uuid.uuid4().hex[:12])
            row=expect(request(route['list'],'POST',data,token),201,'Outage seed')
            targets.append((route,route['list']+row['id']))
        cache=manifest['cache']['service']
        try:
            cmd(compose+['stop',cache])
            for route,item in targets:
                cache_status(request(item,token=token),'BYPASS','Redis outage read')
                expect(request(item,'PUT',sample(route['schema'],'recovery-'+uuid.uuid4().hex[:12]),token),200,'Redis outage write')
        finally:
            cmd(compose+['start',cache])
        deadline=time.time()+30
        for route,item in targets:
            while True:
                result=request(item,token=token)
                expect(result,200,'Recovered read')
                if result[2].get('X-Cache')=='HIT':break
                assert_(time.time()<deadline,'Cache did not recover after restart')
                time.sleep(.2)
            expect(request(item,'DELETE',token=token),200,'Outage cleanup')
    check('Redis outage falls back to database; restart recovers all languages',outage)
