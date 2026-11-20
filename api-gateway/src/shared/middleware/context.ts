import * as telemetry from '../monitoring/telemetry';
import {AsyncLocalStorage} from 'async_hooks';
import {randomUUID} from 'crypto';
import {Request,Response,NextFunction} from 'express';

export interface RequestContext {requestId:string; principal?:string;}
export const requestContext=new AsyncLocalStorage<RequestContext>();
const windows=new Map<string,{count:number;expires:number}>();
const limit=Math.max(0,Number(process.env.RATE_LIMIT_PER_MINUTE||0));

/** Process-local optional limiting; authorization decisions are never cached. */
export function contextMiddleware(req:Request,res:Response,next:NextFunction){
  const supplied=req.header('X-Request-ID')||'';
  const requestId=/^[A-Za-z0-9._-]{1,128}$/.test(supplied)?supplied:randomUUID();
  req.headers['x-request-id']=requestId;res.setHeader('X-Request-ID',requestId);
  const started=performance.now(),path=req.path,tracked=telemetry.begin(path);
  res.once('finish',()=>{
    telemetry.finish(tracked,req.method,path,res.statusCode,(performance.now()-started)/1000,String(res.getHeader('X-Cache')||''));
    if(process.env.REQUEST_LOGGING!=='false'&&!['/health','/metrics'].includes(path))console.log(JSON.stringify({event:'http_request',timestamp:new Date().toISOString(),level:res.statusCode>=500?'error':'info',service:process.env.SERVICE_NAME,instance:process.env.HOSTNAME,requestId,method:telemetry.methodName(req.method),route:telemetry.routeName(path),status:res.statusCode,durationMs:Math.round(performance.now()-started)}));
  });
  if(limit>0&&req.path!=='/health'){
    const now=Date.now(),key=req.socket.remoteAddress||'unknown';
    let window=windows.get(key);
    if(!window||window.expires<=now){window={count:0,expires:now+60000};windows.set(key,window);}
    if(windows.size>10000)windows.delete(windows.keys().next().value!);
    if(++window.count>limit){res.setHeader('Retry-After',Math.max(1,Math.ceil((window.expires-now)/1000)));res.status(429).json({error:'Rate limit exceeded',requestId});return;}
  }
  requestContext.run({requestId},next);
}
