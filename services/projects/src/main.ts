import 'reflect-metadata';
import {contextMiddleware,requestContext} from './shared/middleware/context';
import {AppModule} from './app.module';
import {QueryExceptionFilter} from './shared/http/errors';
import {NestFactory} from '@nestjs/core';
import {Request,Response,NextFunction} from 'express';
import {readFileSync} from 'fs';
import {timingSafeEqual} from 'crypto';
import {Store} from './shared/db/store';
import {ApiKeys,KeyError,requiredScope} from './shared/security/api-keys';
const cfg=JSON.parse(readFileSync('component.json','utf8'));
const env=process.env;
const equal=(a:string,b:string)=>Buffer.byteLength(a)===Buffer.byteLength(b)&&timingSafeEqual(Buffer.from(a),Buffer.from(b));
const urls=(s:string)=>(env[s.toUpperCase().replace(/-/g,'_')+'_MICROSERVICE_URLS']||env[s.toUpperCase().replace(/-/g,'_')+'_MICROSERVICE_URL']||'').split(',');
const counts:Record<string,number>={};
import {registry} from './shared/monitoring/telemetry';
const identityPath=(p:string)=>['/auth/login','/auth/refresh','/auth/logout','/auth/me','/auth/roles','/auth/accounts'].includes(p)||p.startsWith('/auth/roles/')||p.startsWith('/auth/accounts/');
async function main() {
  const app=await NestFactory.create(AppModule,{bodyParser:false});
  const store=app.get(Store);
  const keys=cfg.role==='gateway'?new ApiKeys(store,cfg.scopes):null;
  app.useGlobalFilters(new QueryExceptionFilter());
  app.enableShutdownHooks();
  const express=require('express'); const server=app.getHttpAdapter().getInstance();
  server.use(contextMiddleware);
  server.use(express.json({limit:Number(env.REQUEST_MAX_BYTES||1048576)}));
  server.use((req:Request,res:Response,next:NextFunction)=>{res.setHeader('X-Instance-ID',env.HOSTNAME||cfg.name);next();});
  server.get('/metrics',async(req:Request,res:Response)=>{if(env.MONITORING_TOKEN&&!equal(req.get('authorization')||'','Bearer '+env.MONITORING_TOKEN))return res.sendStatus(401);res.setHeader('Content-Type',registry.contentType);res.send(await registry.metrics());});
  server.get('/health',(_req:Request,res:Response)=>res.json({status:'ok',service:cfg.name}));
  server.get('/openapi.json',(_req:Request,res:Response)=>res.json(cfg.spec));
  const handle=(fn:any)=>(req:Request,res:Response,next:NextFunction)=>Promise.resolve(fn(req,res,next)).catch(next);
  if(cfg.role==='gateway') {
    server.use(handle(async(req:Request,res:Response,next:NextFunction)=>{
      if(!identityPath(req.path))return next();
      try{const headers:Record<string,string>={'Content-Type':'application/json','X-Huza-Internal-Token':env.API_KEY_AUTH_SECRET!,'X-Huza-Peer':req.socket.remoteAddress||'unknown'};for(const h of ['authorization','cookie','origin','x-api-key'])if(req.headers[h])headers[h]=String(req.headers[h]);
        const response=await fetch(env.IDENTITY_URL+req.path,{method:req.method,headers,body:['GET','HEAD'].includes(req.method)?undefined:JSON.stringify(req.body),signal:AbortSignal.timeout(10000),redirect:'manual'});
        for(const h of ['content-type','set-cookie','cache-control','pragma'])if(response.headers.get(h))res.setHeader(h,response.headers.get(h)!);res.status(response.status).send(await response.text());
      }catch{res.status(503).json({error:'Identity authority unavailable'});}
    }));
    server.get('/api/docs.json',handle(async (_req:Request,res:Response)=>{
      const merged:any=JSON.parse(JSON.stringify(cfg.authSpec));
      for(const s of cfg.services){const response=await fetch(urls(s)[0]+'/openapi.json',{signal:AbortSignal.timeout(10000)});if(!response.ok) throw Error('Upstream spec unavailable: '+s);const spec=JSON.parse(JSON.stringify(await response.json()).replaceAll('#/components/schemas/','#/components/schemas/'+s+'__'));for(const [p,ops] of Object.entries(spec.paths) as any){for(const op of Object.values(ops) as any)op.operationId=s+'__'+op.operationId;merged.paths['/api/'+s+p]=ops;}for(const [k,v] of Object.entries(spec.components.schemas))merged.components.schemas[s+'__'+k]=v;merged.tags.push({name:s});}res.json(merged);
    }));
    server.get('/api/docs',(_req:Request,res:Response)=>res.type('html').send('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>API documentation</title><link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css"></head><body><div id="swagger-ui"></div><script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script><script>SwaggerUIBundle({url:"/api/docs.json",dom_id:"#swagger-ui"})</script></body></html>'));
  }
  if(keys) server.post('/internal/api-keys/authorize',handle(async(req:Request,res:Response)=>{
    if(!env.API_KEY_AUTH_SECRET||!equal(String(req.headers['x-huza-internal-token']||''),env.API_KEY_AUTH_SECRET))return res.status(401).json({error:'Unauthorized'});
    try {const status=await keys.authorize(req.body.key,req.body.scope);res.status(status).json({allowed:status===200});}catch{res.status(503).json({error:'Key registry unavailable'});}
  }));
  server.use(handle(async(req:Request,res:Response,next:NextFunction)=>{
    const auth=req.headers.authorization||'',key=req.headers['x-api-key'];
    if(key!==undefined){
      if(auth||typeof key!=='string')return res.status(401).json({error:'Ambiguous credentials'});
      try {
        const scope=requiredScope(cfg,req.path,req.method);
        const status=keys?await keys.authorize(key,scope):(await fetch(env.AUTHORITY_URL+'/internal/api-keys/authorize',{method:'POST',headers:{'Content-Type':'application/json','X-Huza-Internal-Token':env.API_KEY_AUTH_SECRET!},body:JSON.stringify({key,scope}),signal:AbortSignal.timeout(5000)})).status;
        if(status!==200)return res.status([401,403].includes(status)?status:503).json({error:status===403?'Insufficient scope':status===401?'Invalid API key':'Key authority unavailable'});
        (req as any).huzaPrincipal='api-key';
      }catch{return res.status(503).json({error:'Key authority unavailable'});}
    }else{
      if(!auth.startsWith('Bearer '))return res.status(401).json({error:'Unauthorized'});
      try{const scope=keys&&req.path.startsWith('/auth/')?'@admin':requiredScope(cfg,req.path,req.method);const response=await fetch(env.IDENTITY_URL+'/authorize',{method:'POST',headers:{'Content-Type':'application/json','X-Huza-Internal-Token':env.API_KEY_AUTH_SECRET!},body:JSON.stringify({token:auth.slice(7),scope}),signal:AbortSignal.timeout(5000)});
        if(response.status!==200)return res.status([401,403].includes(response.status)?response.status:503).json({error:'Access denied'});const result=await response.json() as {admin:boolean};(req as any).huzaPrincipal=result.admin?'admin':'user';
      }catch{return res.status(503).json({error:'Identity authority unavailable'});}
    }
    const context=requestContext.getStore();if(context)context.principal=(req as any).huzaPrincipal;
    // Continue only after authentication and the current scope check complete.
    next();
  }));
  if(cfg.role==='gateway') {
    server.use('/auth',(req:Request,res:Response,next:NextFunction)=>{if((req as any).huzaPrincipal!=='admin')return res.status(403).json({error:'Administrator access required'});next();});
    server.get('/auth/scopes',(_req:Request,res:Response)=>res.json(cfg.scopes));
    server.all(['/auth/api-keys','/auth/api-keys/:id'],handle(async(req:Request,res:Response)=>{res.setHeader('Cache-Control','no-store');try{const [status,body]=await keys!.manage(req.method,req.params.id,req.body);res.status(status).json(body);}catch(e){if(e instanceof KeyError)return res.status(e.status).json({error:e.message});res.status(503).json({error:'Key registry unavailable'});}}));
  }
  server.use((err:any,_req:Request,res:Response,_next:NextFunction)=>{console.error(err);res.status(500).json({error:'Request failed'});});
  await app.listen(Number(env.PORT||8000),'0.0.0.0');
}
main().catch(e=>{console.error(e);process.exit(1);});
