"""Persistent integration keys. Permissions are read on every request."""
import hashlib
import hmac
import re
import secrets
import time
import threading

class KeyErrorResponse(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message

class ApiKeys:
    def __init__(self, store, scopes):
        self.store, self.scopes = store, set(scopes)
        self.lock = threading.RLock()

    def public(self, row):
        return {k:v for k,v in row.items() if k != 'hash'}

    def fields(self, body):
        if not isinstance(body, dict) or not isinstance(body.get('name'), str) or not 1 <= len(body['name'].strip()) <= 120:
            raise KeyErrorResponse(400, 'A name of 1–120 characters is required')
        scopes = body.get('scopes')
        if not isinstance(scopes, list) or any(not isinstance(s,str) or s not in self.scopes for s in scopes):
            raise KeyErrorResponse(400, 'Scopes must be an array of catalog entries')
        expiry = body.get('expiresAt')
        if expiry is not None and (type(expiry) is not int or expiry <= int(time.time())):
            raise KeyErrorResponse(400, 'expiresAt must be a future Unix timestamp or null')
        return {'name':body['name'].strip(), 'scopes':sorted(set(scopes)), 'expiresAt':expiry}

    def manage(self, method, id, body):
        with self.lock:
            if not id and method == 'GET':
                return 200, [self.public(r) for r in self.store.list('api_keys')]
            if not id and method == 'POST':
                fields = self.fields(body)
                secret = secrets.token_hex(32)
                row = self.store.save('api_keys', {**fields, 'hash':hashlib.sha256(secret.encode()).hexdigest(), 'createdAt':int(time.time()), 'revoked':False})
                return 201, {**self.public(row), 'key':'hp_'+row['id']+'_'+secret}
            if id and method in ('PUT','DELETE'):
                rows = self.store.list('api_keys', id)
                if not rows: raise KeyErrorResponse(404, 'Key not found')
                row = rows[0]
                if method == 'PUT':
                    if row['revoked']: raise KeyErrorResponse(409, 'Revoked keys cannot be changed')
                    row.update(self.fields(body))
                else: row['revoked'] = True
                return 200, self.public(self.store.save('api_keys', row, id))
            raise KeyErrorResponse(405, 'Method not allowed')

    def authorize(self, key, scope):
        match = re.fullmatch(r'hp_([0-9a-f-]{36})_([0-9a-f]{64})', key or '')
        if not match: return 401
        rows = self.store.list('api_keys', match[1])
        if not rows: return 401
        row = rows[0]
        if not hmac.compare_digest(row['hash'], hashlib.sha256(match[2].encode()).hexdigest()) or row['revoked'] or (row['expiresAt'] is not None and row['expiresAt'] <= time.time()): return 401
        return 200 if scope and scope in row['scopes'] and scope in self.scopes else 403

def required_scope(cfg, path, method):
    parts = path.strip('/').split('/')
    if cfg['role'] == 'gateway':
        if len(parts) < 3 or parts[0] != 'api' or parts[1] not in cfg['services']: return ''
        service, parts = parts[1], parts[2:]
    else: service = cfg['name']
    query = method == 'POST' and len(parts) == 2 and parts[1] in ('_query', '_aggregate')
    action = 'read' if query else {'GET':'read','POST':'create','PUT':'update','DELETE':'delete'}.get(method)
    if not action or len(parts) not in (1,2) or (method == 'POST' and len(parts)!=1 and not query) or (method in ('PUT','DELETE') and len(parts)!=2): return ''
    if cfg['role'] == 'service' and parts[0] not in cfg['modules']: return ''
    return service+':'+parts[0]+':'+action
