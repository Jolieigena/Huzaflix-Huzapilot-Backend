import {event} from './monitoring/telemetry';
import Redis from 'ioredis';
import {createHash,randomUUID} from 'node:crypto';
import {Request,Response} from 'express';
const READ="local version=redis.call('GET',KEYS[1]); if not version then version=ARGV[1]; redis.call('SET',KEYS[1],version) end; local value=redis.call('GET',KEYS[2]); if value and string.sub(value,1,string.len(version)+1)==version..'\\n' then return {version,string.sub(value,string.len(version)+2)} end; return {version,false}";
const FILL="if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('SET',KEYS[2],ARGV[1]..'\\n'..ARGV[2],'PX',ARGV[3]) end; return false";
const hash=(value:string)=>createHash('sha256').update(value).digest('hex');
const bounded=(name:string,fallback:number,min:number,max:number)=>{const value=Number(process.env[name]??fallback);return Number.isFinite(value)?Math.min(max,Math.max(min,Math.floor(value))):fallback;};
/** Optional cache; never caches authorization. DB/Redis are not one transaction. */
export class ResponseCache {
  private readonly client:Redis|null;
  private readonly ttl=bounded('CACHE_TTL_SECONDS',30,1,300)*1000;
  private readonly maxBytes=bounded('CACHE_MAX_BYTES',1048576,1024,8388608);
  constructor(){
    this.client=process.env.REDIS_URL&&process.env.CACHE_ENABLED!=='false'?new Redis(process.env.REDIS_URL,{connectTimeout:200,commandTimeout:200,maxRetriesPerRequest:0,enableOfflineQueue:false,retryStrategy:()=>1000}):null;
    this.client?.on('error',()=>{}); // No credentials or query contents in logs.
  }
  private prefix(model:string){return 'huza:cache:{'+hash(JSON.stringify([process.env.CACHE_NAMESPACE??'huzapilot',process.env.SERVICE_NAME??'service',model]))+'}';}
  async invalidate(model:string){if(this.client)try{await this.client.set(this.prefix(model)+':version',randomUUID());event('cache','invalidate','success',model);}catch{event('cache','invalidate','error',model); /* committed DB result is authoritative */ }}
  async read<T>(model:string,fingerprint:string,load:()=>Promise<T>,bypass=false):Promise<{value:T;status:string}>{
    if(!this.client||bypass||this.client.status!=='ready')return {value:await load(),status:'BYPASS'};
    const start=performance.now(),prefix=this.prefix(model),keys=[prefix+':version',prefix+':entry:'+hash(fingerprint)];let version:string;
    try{const reply=await this.client.eval(READ,2,...keys,randomUUID()) as [string,string|null];version=reply[0];if(reply[1]!==null){try{return {value:JSON.parse(reply[1]) as T,status:'HIT'};}catch{}}}catch{event('cache','read-error','error',model);return {value:await load(),status:'BYPASS'};}
    const value=await load(),ttl=this.ttl-Math.ceil(performance.now()-start);
    try{const payload=JSON.stringify(value);if(ttl>0&&payload!==undefined&&Buffer.byteLength(payload)<=this.maxBytes)await this.client.eval(FILL,2,...keys,version,payload,ttl);}catch{event('cache','fill','error',model);}
    return {value,status:'MISS'};
  }
  close(){this.client?.disconnect();}
}
export async function cached<T>(cache:ResponseCache,model:string,req:Request,res:Response,body:unknown,load:()=>Promise<T>):Promise<T>{
  const headers=[...new Set(['authorization','x-api-key','accept-language','origin',...(process.env.CACHE_VARY_HEADERS??'').toLowerCase().split(',')].map(h=>h.trim().toLowerCase()).filter(Boolean))];
  const fingerprint=JSON.stringify([req.method,req.originalUrl,headers.map(h=>[h,req.get(h)??'']),body]);
  const control=(req.get('cache-control')??'').toLowerCase();
  const result=await cache.read(model,fingerprint,load,control.includes('no-cache')||control.includes('no-store')||req.get('x-cache-bypass')==='1'||req.query.cache==='miss');
  res.setHeader('X-Cache',result.status);res.setHeader('Cache-Control','private, no-store');return result.value;
}
