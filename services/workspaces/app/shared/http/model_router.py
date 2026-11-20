"""Shared HTTP CRUD/query controller; generated APIRouters register each model."""
from fastapi import APIRouter, Request, Response
from ..cache import cached
from ..db.query import QueryError


def model_router(service):
    router = APIRouter(prefix='/' + service.name, tags=[service.name])

    @router.get('/')
    def listing(request: Request, response: Response): return cached(service,request,response,None,service.find)

    @router.post('/', status_code=201)
    def create(body: dict): return service.create(body)

    @router.post('/_query')
    def query(body: dict, request: Request, response: Response):
        if set(body) - {'filter', 'options'}: raise QueryError('Unknown query fields')
        if any(not isinstance(body[k], dict) for k in body): raise QueryError('Filter and options must be objects')
        return cached(service,request,response,body,lambda: service.find(body.get('filter', {}), body.get('options', {})))

    @router.post('/_aggregate')
    def aggregation(body: dict, request: Request, response: Response):
        if set(body) != {'pipeline'}: raise QueryError('Expected pipeline')
        return cached(service,request,response,body,lambda: service.aggregate(body['pipeline']))

    @router.get('/{id}')
    def detail(id: str, request: Request, response: Response):
        def load():
            row = service.find_by_id(id)
            if row is None: raise QueryError('Not found', 'NOT_FOUND', 404)
            return row
        return cached(service,request,response,None,load)

    @router.put('/{id}')
    def update(id: str, body: dict): return service.update_by_id(id, body)

    @router.delete('/{id}')
    def delete(id: str): return service.delete_by_id(id)

    return router
