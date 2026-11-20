"""Pure ASGI request context, bounded body, optional local rate limit and JSON logs."""
from app.shared import monitoring as telemetry
import datetime
import contextvars
import json
import logging
import os
import re
import time
import uuid

request_context = contextvars.ContextVar('huza_request_context', default=None)
logger = logging.getLogger('huza.http')


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app
        self.windows = {}
        self.limit = max(0, int(os.getenv('RATE_LIMIT_PER_MINUTE', '0')))
        self.body_limit = max(1024, int(os.getenv('REQUEST_MAX_BYTES', '1048576')))

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        supplied = headers.get(b'x-request-id', b'').decode('ascii', 'ignore')
        request_id = supplied if re.fullmatch(r'[A-Za-z0-9._-]{1,128}', supplied) else str(uuid.uuid4())
        scope['headers'] = [(k, v) for k, v in scope['headers'] if k.lower() != b'x-request-id'] + [(b'x-request-id', request_id.encode())]
        started, status = time.monotonic(), 500
        tracked=telemetry.begin(scope['path']);cache=None
        token = request_context.set({'requestId': request_id})

        async def respond(code, message, extra=()):
            nonlocal status
            status = code
            await send({'type': 'http.response.start', 'status': code, 'headers': [(b'content-type', b'application/json'), (b'x-request-id', request_id.encode()), *extra]})
            await send({'type': 'http.response.body', 'body': json.dumps({'error': message, 'requestId': request_id}).encode()})

        async def wrapped_send(message):
            nonlocal status, cache
            if message['type'] == 'http.response.start':
                status = message['status']
                cache=dict(message.get('headers',[])).get(b'x-cache',b'').decode()
                message['headers'] = [(k, v) for k, v in message.get('headers', []) if k.lower() != b'x-request-id'] + [(b'x-request-id', request_id.encode())]
            await send(message)

        try:
            if self.limit and scope['path'] != '/health':
                key = (scope.get('client') or ['unknown'])[0]
                now = time.monotonic()
                count, expiry = self.windows.get(key, (0, now + 60))
                if expiry <= now: count, expiry = 0, now + 60
                self.windows[key] = count + 1, expiry
                if len(self.windows) > 10000: self.windows.pop(next(iter(self.windows)))
                if count >= self.limit: return await respond(429, 'Rate limit exceeded', [(b'retry-after', str(max(1, int(expiry - now))).encode())])
            chunks, size = [], 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect': return
                chunk = message.get('body', b''); size += len(chunk)
                if size > self.body_limit: return await respond(413, 'Request body exceeds limit')
                chunks.append(chunk)
                if not message.get('more_body', False): break
            body, delivered = b''.join(chunks), False
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': body, 'more_body': False}
                return await receive()
            await self.app(scope, replay, wrapped_send)
        finally:
            telemetry.finish(tracked,scope['method'],scope['path'],status,time.monotonic()-started,cache)
            if os.getenv('REQUEST_LOGGING') != 'false' and scope['path'] not in ('/health','/metrics'):
                print(json.dumps({'event':'http_request','timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'level':'error' if status>=500 else 'info','service':os.getenv('SERVICE_NAME'),'instance':os.getenv('HOSTNAME'),'requestId':request_id,'method':scope['method'] if scope['method'] in ('GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS') else 'OTHER','route':telemetry.route_name(scope['path']),'status':status,'durationMs':round((time.monotonic()-started)*1000)}),flush=True)
            request_context.reset(token)
