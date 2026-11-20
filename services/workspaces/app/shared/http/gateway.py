"""Round-robin forwarding reused by explicit generated service routers."""
import os
import httpx
from fastapi.responses import Response, JSONResponse

_counts = {}


async def forward(service, path, request):
    key = service.upper().replace('-', '_')
    instances = [url for url in os.getenv(key + '_MICROSERVICE_URLS', os.getenv(key + '_MICROSERVICE_URL', '')).split(',') if url]
    if not instances: return JSONResponse({'error': 'No service instances configured'}, status_code=503)
    next_index = _counts.get(service, 0)
    _counts[service] = (next_index + 1) % len(instances)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(request.method, instances[next_index % len(instances)] + '/' + path,
                                        params=request.query_params, content=await request.body(),
                                        headers={k: v for k, v in request.headers.items() if k.lower() in ('authorization', 'x-api-key', 'content-type', 'x-request-id', 'cache-control', 'x-cache-bypass', 'accept-language', 'origin', *(h.strip() for h in os.getenv('CACHE_VARY_HEADERS','').lower().split(',') if h.strip()))})
    return Response(response.content, status_code=response.status_code,
                    headers={k: v for k, v in response.headers.items() if k in ('content-type', 'x-instance-id', 'x-cache', 'cache-control')})
