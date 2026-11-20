import {createHash,randomBytes,timingSafeEqual} from 'crypto';
import {Store} from '../db/store';
export class KeyError extends Error {constructor(public status:number,message:string){super(message);}}
export class ApiKeys {
  private queue:Promise<any>=Promise.resolve();
  constructor(private store:Store,public scopes:string[]){}
  public(row:any){const {hash,...safe}=row;return safe;}
  fields(body:any){
    if(!body||typeof body.name!=='string'||body.name.trim().length<1||body.name.trim().length>120)throw new KeyError(400,'A name of 1–120 characters is required');
    if(!Array.isArray(body.scopes)||body.scopes.some((s:any)=>typeof s!=='string'||!this.scopes.includes(s)))throw new KeyError(400,'Scopes must be an array of catalog entries');
    const expiresAt=body.expiresAt??null;
    if(expiresAt!==null&&(!Number.isSafeInteger(expiresAt)||expiresAt<=Date.now()/1000))throw new KeyError(400,'expiresAt must be a future Unix timestamp or null');
    return {name:body.name.trim(),scopes:[...new Set(body.scopes)].sort(),expiresAt};
  }
  manage(method:string,id:string|undefined,body:any):Promise<[number,any]>{
    const task=this.queue.then(async():Promise<[number,any]>=>{
      if(!id&&method==='GET')return [200,(await this.store.list('api_keys')).map(r=>this.public(r))];
      if(!id&&method==='POST'){
        const fields=this.fields(body),secret=randomBytes(32).toString('hex');
        const row=await this.store.save('api_keys',{...fields,hash:createHash('sha256').update(secret).digest('hex'),createdAt:Math.floor(Date.now()/1000),revoked:false});
        return [201,{...this.public(row),key:'hp_'+row.id+'_'+secret}];
      }
      if(id&&['PUT','DELETE'].includes(method)){
        const row=(await this.store.list('api_keys',id))[0];if(!row)throw new KeyError(404,'Key not found');
        if(method==='PUT'){if(row.revoked)throw new KeyError(409,'Revoked keys cannot be changed');Object.assign(row,this.fields(body));}else row.revoked=true;
        return [200,this.public(await this.store.save('api_keys',row,id))];
      }
      throw new KeyError(405,'Method not allowed');
    });this.queue=task.catch(()=>{});return task;
  }
  async authorize(key:string,scope:string){
    const match=/^hp_([0-9a-f-]{36})_([0-9a-f]{64})$/.exec(key||'');if(!match)return 401;
    const row=(await this.store.list('api_keys',match[1]))[0];if(!row)return 401;
    const actual=createHash('sha256').update(match[2]).digest('hex');
    if(!timingSafeEqual(Buffer.from(row.hash),Buffer.from(actual))||row.revoked||(row.expiresAt!==null&&row.expiresAt<=Date.now()/1000))return 401;
    return scope&&this.scopes.includes(scope)&&row.scopes.includes(scope)?200:403;
  }
}
export function requiredScope(cfg:any,path:string,method:string){
  let parts=path.replace(/^\/+|\/+$/g,'').split('/'),service=cfg.name;
  if(cfg.role==='gateway'){if(parts.length<3||parts[0]!=='api'||!cfg.services.includes(parts[1]))return '';service=parts[1];parts=parts.slice(2);}
  const query=method==='POST'&&parts.length===2&&['_query','_aggregate'].includes(parts[1]);
  const action=query?'read':({GET:'read',POST:'create',PUT:'update',DELETE:'delete'} as Record<string,string>)[method];
  if(!action||![1,2].includes(parts.length)||(method==='POST'&&parts.length!==1&&!query)||(['PUT','DELETE'].includes(method)&&parts.length!==2))return '';
  if(cfg.role==='service'&&!cfg.modules.includes(parts[0]))return '';
  return service+':'+parts[0]+':'+action;
}
