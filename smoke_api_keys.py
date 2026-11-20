"""Integration key checks shared by every generated project's smoke suite."""
import base64
import hashlib
import hmac
import json
import time
import urllib.request
import urllib.error


def run_checks(manifest, env, request, check, assert_, cmd, compose, token, sample, health, verify_persistence):
    all_routes=[(s,r) for s in manifest['services'] for r in s['routes']]
    all_scopes=sorted(f"{s['name']}:{r['module']}:{a}" for s,r in all_routes for a in ('read','create','update','delete'))
    revoked=[]
    def expect(result,status,label):
        assert_(result[0]==status,f'{label}: expected {status}, got {result[0]}')
        return result[1]
    def create(scopes,expiry=None):
        body=expect(request('/auth/api-keys','POST',{'name':'Smoke integration','scopes':scopes,'expiresAt':expiry},token),201,'Create API key')
        assert_('hash' not in body and body['key'].startswith('hp_'),'Creation response shape')
        return body
    def update(key,scopes):
        result=expect(request('/auth/api-keys/'+key['id'],'PUT',{'name':key['name'],'scopes':scopes,'expiresAt':None},token),200,'Update permissions')
        assert_('key' not in result and 'hash' not in result,'Update disclosed secret')
    def direct(container,path,method,key,data=None):
        address=cmd(compose+['port',container,'8000']).strip().splitlines()[0]
        return request(path,method,data,api_key=key,origin='http://'+address)
    def contract():
        catalog=expect(request('/auth/scopes',token=token),200,'Scope catalog');assert_(catalog==all_scopes,'Catalog differs from manifest models')
        spec=expect(request('/api/docs.json'),200,'Swagger')
        assert_(spec['components']['securitySchemes']['apiKeyAuth']=={'type':'apiKey','in':'header','name':'X-API-Key'},'Missing API key scheme')
        for svc,route in all_routes:
            for path,methods in [(route['list'],{'get':'read','post':'create'}),(route['detail'],{'get':'read','put':'update','delete':'delete'})]:
                for method,action in methods.items():
                    op=spec['paths'][path][method]
                    assert_(op['x-required-scope']==f"{svc['name']}:{route['module']}:{action}" and {'apiKeyAuth':[]} in op['security'],'Incorrect route scope')
        expect(request('/auth/api-keys'),401,'Anonymous key management')
        expect(request('/internal/api-keys/authorize','POST',{'key':'fake','scope':all_scopes[0]}),401,'Internal authorization protection')
        for body in [{'name':'bad','scopes':['*']},{'name':'bad','scopes':['unknown:model:read']},{'name':'bad','scopes':'read'},{'name':'','scopes':[]},{'name':'bad','scopes':[],'expiresAt':1}]:
            expect(request('/auth/api-keys','POST',body,token),400,'Invalid key definition')
        enc=lambda data:base64.urlsafe_b64encode(json.dumps(data,separators=(',',':')).encode()).rstrip(b'=')
        raw=enc({'alg':'HS256','typ':'JWT'})+b'.'+enc({'sub':'reader','role':'reader','kind':'access','exp':int(time.time())+300})
        signature=base64.urlsafe_b64encode(hmac.new(env['JWT_SECRET'].encode(),raw,hashlib.sha256).digest()).rstrip(b'=')
        expect(request('/auth/api-keys',token=(raw+b'.'+signature).decode()),401,'Unregistered JWT identity management')
    check('API keys catalog, OpenAPI and admin-only management',contract)
    for svc,route in all_routes:
        def model(svc=svc,route=route):
            prefix=svc['name']+':'+route['module']+':';read=prefix+'read';path=route['list'];local='/'+route['module']+'/'
            payload=sample(route['schema'],'key-'+str(time.time_ns()))
            seed=expect(request(path,'POST',payload,token),201,'Admin seed');item=path+seed['id']
            key=create([]);secret=key['key']
            expect(request(path,api_key=secret),403,'Empty scopes deny')
            expect(request(path,api_key='invalid'),401,'Invalid API key')
            changed=secret[:-1]+('0' if secret[-1]!='0' else '1');expect(request(path,api_key=changed),401,'Tampered secret')
            expect(request(path,token=token,api_key=secret),401,'Ambiguous credentials')
            update(key,[read]);expect(request(path,api_key=secret),200,'Read granted');expect(request(item,api_key=secret),200,'Detail granted')
            for method,url in [('POST',path),('PUT',item),('DELETE',item)]:expect(request(url,method,payload if method!='DELETE' else None,api_key=secret),403,'Read cannot '+method)
            for other_s,other_r in all_routes:
                if (other_s['name'],other_r['module'])!=(svc['name'],route['module']):expect(request(other_r['list'],api_key=secret),403,'Cross-service/model isolation')
            expect(request('/auth/api-keys',api_key=secret),403,'API key cannot manage keys')
            expect(request('/auth/scopes',api_key=secret),403,'API key cannot administer scopes')
            expect(request('/auth/refresh','POST',{'refresh_token':secret}),401,'API key cannot exchange for JWT')
            for container in svc['containers']:
                expect(direct(container,local,'GET',secret),200,'Direct instance read')
                expect(direct(container,local,'POST',secret,payload),403,'Direct instance write denied')
            update(key,[prefix+a for a in ('read','create','update','delete')])
            record=expect(request(path,'POST',payload,api_key=secret),201,'Expanded create');target=path+record['id'];verify_persistence(svc,route,record['id'],payload)
            edited=sample(route['schema'],'changed-'+str(time.time_ns()));expect(request(target,'PUT',edited,api_key=secret),200,'Expanded update')
            actual=expect(request(target,api_key=secret),200,'Read updated record');assert_(all(actual[k]==v for k,v in edited.items()),'API key update did not persist')
            expect(request(target,'DELETE',api_key=secret),200,'Expanded delete');expect(request(target,api_key=secret),404,'Deleted API key record')
            update(key,[read])
            for method,url in [('POST',path),('PUT',item),('DELETE',item)]:expect(request(url,method,payload if method!='DELETE' else None,api_key=secret),403,'Reduced permissions deny '+method)
            seen=set()
            for _ in range(svc['instances']*2):
                result=request(path,api_key=secret);expect(result,200,'Scoped round robin');seen.add(result[2].get('X-Instance-ID'))
            assert_(None not in seen and len(seen)==svc['instances'],'API-key requests did not reach every instance')
            for container in svc['containers']:expect(direct(container,local,'POST',secret,payload),403,'Reduction enforced on direct replica')
            listing=expect(request('/auth/api-keys',token=token),200,'List keys');assert_(all('key' not in r and 'hash' not in r for r in listing),'List disclosed secret')
            stored=json.loads(cmd(compose+['exec','-T','api-gateway','sqlite3','/data/app.sqlite',f"SELECT data FROM hp_api_keys WHERE id = '{key['id']}'"]))
            assert_(stored['hash']==hashlib.sha256(secret.rsplit('_',1)[1].encode()).hexdigest() and secret not in json.dumps(stored),'Key storage is not hashed')
            expect(request('/auth/api-keys/'+key['id'],'DELETE',token=token),200,'Revoke')
            expect(request(path,api_key=secret),401,'Revoked key')
            expect(request('/auth/api-keys/'+key['id'],'PUT',{'name':'Cannot revive','scopes':[read]},token),409,'Revocation is permanent')
            for container in svc['containers']:expect(direct(container,local,'GET',secret),401,'Revocation on every replica')
            revoked.append((path,secret));expect(request(item,'DELETE',token=token),200,'Seed cleanup')
        check(f"API key {svc['lang']}×{svc['database']} {svc['name']}/{route['module']} CRUD, dynamic scopes, isolation, replicas and revocation",model)
    def lifecycle():
        svc,route=all_routes[0];scope=svc['name']+':'+route['module']+':read'
        expired=create([scope],int(time.time())+3);expect(request(route['list'],api_key=expired['key']),200,'Before expiry');time.sleep(4);expect(request(route['list'],api_key=expired['key']),401,'Expired key')
        persistent=create([scope]);update(persistent,[scope])
        try:
            cmd(compose+['stop','api-gateway'],timeout=90)
            for service,rt in all_routes:
                for container in service['containers']:expect(direct(container,'/'+rt['module']+'/','GET',persistent['key']),503,'Authority outage must fail closed')
        finally:
            cmd(compose+['start','api-gateway'],timeout=90);health()
        expect(request(route['list'],api_key=persistent['key']),200,'Key survives gateway restart')
        for path,secret in revoked:expect(request(path,api_key=secret),401,'Revocation survives restart')
        expect(request(route['list'],api_key=expired['key']),401,'Expiry survives restart')
        expect(request('/auth/api-keys/'+persistent['id'],'DELETE',token=token),200,'Persistent key cleanup')
    check('API key expiry, fail-closed authority outage and restart persistence',lifecycle)
