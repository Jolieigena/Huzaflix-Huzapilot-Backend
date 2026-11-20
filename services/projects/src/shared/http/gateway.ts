import {Injectable} from '@nestjs/common';
import {Request,Response} from 'express';

@Injectable()
export class GatewayProxy {
  private readonly counts=new Map<string,number>();
  async forward(service:string,req:Request,res:Response){
    const key=service.toUpperCase().replace(/-/g,'_');
    const all=(process.env[key+'_MICROSERVICE_URLS']||process.env[key+'_MICROSERVICE_URL']||'').split(',').filter(Boolean);
    if(!all.length)return res.status(503).json({error:'No service instances configured'});
    const next=this.counts.get(service)||0;this.counts.set(service,(next+1)%all.length);
    const cacheHeaders:Record<string,string>={};
    for(const rawName of ['cache-control','x-cache-bypass','accept-language','origin',...(process.env.CACHE_VARY_HEADERS??'').split(',')]){const name=rawName.trim().toLowerCase();if(!name||['authorization','x-api-key','content-type','x-request-id'].includes(name))continue;const value=req.get(name);if(value)cacheHeaders[name]=value;}
    const response=await fetch(all[next%all.length]+req.originalUrl.slice(('/api/'+service).length),{
      method:req.method,headers:{...cacheHeaders,'Content-Type':'application/json','X-Request-ID':String(req.headers['x-request-id']||''),...(req.headers.authorization?{Authorization:req.headers.authorization}:{'X-API-Key':String(req.headers['x-api-key'])})},
      body:['GET','HEAD'].includes(req.method)?undefined:JSON.stringify(req.body),signal:AbortSignal.timeout(30000),redirect:'manual'});
    for(const name of ['x-cache','cache-control']){const value=response.headers.get(name);if(value)res.setHeader(name,value);}
    return res.status(response.status).set('Content-Type','application/json').set('X-Instance-ID',response.headers.get('x-instance-id')||'').send(await response.text());
  }
}
