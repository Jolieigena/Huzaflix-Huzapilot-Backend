/** Bounded labels; no request credentials, bodies, IDs or raw query text. */
import {Registry,Counter,Histogram,Gauge,collectDefaultMetrics} from 'prom-client';
import {readFileSync,statSync} from 'node:fs';
export const enabled=process.env.MONITORING_ENABLED!=='false';
export const registry=new Registry();
if(enabled)collectDefaultMetrics({register:registry});
const requests=new Counter({name:'http_requests_total',help:'Completed application requests',labelNames:['method','route','status_code'],registers:[registry]});
const duration=new Histogram({name:'http_request_duration_seconds',help:'End to end HTTP duration',labelNames:['method','route','status_code'],buckets:[.005,.025,.1,.25,.5,1,2.5,5,10,30],registers:[registry]});
const active=new Gauge({name:'http_requests_in_flight',help:'Active application requests',registers:[registry]});
const operations=new Histogram({name:'huza_db_operation_duration_seconds',help:'Shared store calls, including nested calls',labelNames:['model','operation','outcome'],buckets:[.001,.005,.025,.1,.5,1,5,30],registers:[registry]});
const events=new Counter({name:'huza_events_total',help:'Shared infrastructure outcomes',labelNames:['component','operation','outcome','model'],registers:[registry]});
let routes:string[]=[];try{routes=JSON.parse(readFileSync('telemetry-routes.json','utf8'));}catch{}
const patterns=routes.map(route=>({route,re:new RegExp('^'+route.split('/').map(s=>s.startsWith('{')?'[^/]+':s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('/')+'/?$')}));
export const routeName=(path:string)=>patterns.find(p=>p.re.test(path))?.route||'unmatched';
export const methodName=(method:string)=>['GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'].includes(method)?method:'OTHER';
export function begin(path:string){const tracked=enabled&&!['/health','/metrics','/actuator/prometheus'].includes(path);if(tracked)active.inc();return tracked;}
export function finish(tracked:boolean,method:string,path:string,status:number,seconds:number,cache?:string){if(!tracked)return;active.dec();const labels={method:methodName(method),route:routeName(path)};requests.inc({...labels,status_code:String(status)});duration.observe({...labels,status_code:String(status)},seconds);if(cache&&['HIT','MISS','BYPASS'].includes(cache))event('cache','read',cache);}
export function event(component:string,operation:string,outcome:string,model=''){if(enabled)events.inc({component,operation,outcome,model});}
export async function observe<T>(model:string,operation:string,run:()=>Promise<T>):Promise<T>{if(!enabled)return run();const start=performance.now();let outcome='success';try{return await run();}catch(e){outcome='error';throw e;}finally{operations.observe({model,operation,outcome},(performance.now()-start)/1000);}}

if(process.env.DB_TYPE==='sqlite')new Gauge({name:'huza_sqlite_file_bytes',help:'SQLite data and WAL file sizes',labelNames:['file'],registers:[registry],collect(){for(const file of ['app.sqlite','app.sqlite-wal']){let size=0;try{size=statSync('/data/'+file).size;}catch{}this.set({file},size);}}});
