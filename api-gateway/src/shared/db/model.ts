import Ajv, {ValidateFunction} from 'ajv';
import addFormats from 'ajv-formats';
import {plan,candidateFilter,candidateIds} from './planner';
import {Store} from './store';
import {Document,Value,QueryError,aggregate,find,validatePipeline,validateValue,updated,distinct,canonical} from './query';

/** Shared facade. Override lifecycle hooks in a generated module's service. */
export class Model {
  private readonly validate: ValidateFunction;
  readonly maxRows=Math.min(10000,Math.max(1,Number(process.env.QUERY_MAX_ROWS||1000)));
  readonly maxBytes=Math.min(67108864,Math.max(1024,Number(process.env.QUERY_MAX_BYTES||8388608)));
  constructor(readonly store:Store,readonly name:string,readonly schema:Record<string,unknown>) {
    const ajv=new Ajv({allErrors:true,strict:false,ownProperties:true});addFormats(ajv);
    this.validate=ajv.compile(schema);
  }
  async beforeCreate(data:Document):Promise<Document>{return data;}
  async afterCreate(_data:Document):Promise<void>{}
  async beforeUpdate(_current:Document,data:Document):Promise<Document>{return data;}
  async afterUpdate(_data:Document):Promise<void>{}
  async beforeDelete(_current:Document):Promise<void>{}
  async afterDelete(_previous:Document):Promise<void>{}
  private valid(input:Document):Document {
    if(!input||typeof input!=='object'||Array.isArray(input))throw new QueryError('Record must be an object','VALIDATION_ERROR',422);
    const data=structuredClone(input);validateValue(data);delete data.id;delete data._id;
    if(!this.validate(data))throw new QueryError(this.validate.errors?.map(e=>`${e.instancePath}: ${e.message}`).join('; ')||'Invalid record','VALIDATION_ERROR',422);
    return data;
  }
  async find(filter:Document={},options:Document={}):Promise<Document[]> {
    find([],filter,options);
    const rows=await this.store.query(this.name,this.schema,filter,options,this.maxRows,this.maxBytes);
    if(rows!==null)return rows as Document[];
    const candidate=this.store.kind==='cassandra'?{}:candidateFilter(this.store.kind,this.schema,filter);
    const bounded=Object.keys(candidate).length?await this.store.query(this.name,this.schema,candidate,{},this.maxRows,this.maxBytes):null;
    return find(bounded===null?await this.store.scan(this.name,this.maxRows,this.maxBytes):bounded as Document[],filter,options);
  }
  /** Planner report only: native execution does not guarantee index use. */
  explain(filter:Document={},options:Document={}):Document{
    find([],filter,options);const kind=this.store.kind,native=kind==='cassandra'?candidateIds(filter)!==null:plan(kind==='mongodb'?'postgres':kind,this.schema,filter,options)!==null;
    const candidate=native||kind==='cassandra'?{}:candidateFilter(kind,this.schema,filter);
    return {database:kind,execution:native?(kind==='cassandra'?'primary-key':'native'):Object.keys(candidate).length?'native-prefilter':'bounded-scan',maxRows:this.maxRows,maxBytes:this.maxBytes,atomicWrites:'single-record',bulkAtomic:false,upsert:'id',crossModelHttpLookup:false};
  }
  async findPage(filter:Document={},size=50,after?:string,total=false):Promise<Document>{
    if(!Number.isInteger(size)||size<1||size>=this.maxRows)throw new QueryError('Page size must be smaller than QUERY_MAX_ROWS');
    if(after!==undefined&&(typeof after!=='string'||!after||after.length>36))throw new QueryError('Invalid page cursor');
    const predicate=after===undefined?filter:{$and:[filter,{id:{$gt:after}}]},rows=await this.find(predicate,{sort:{id:1},limit:size+1}),items=rows.slice(0,size),hasMore=rows.length>size;
    const result:Document={items,hasMore,nextAfter:hasMore?items.at(-1)!.id:null};if(total)result.total=await this.countDocuments(filter);return result;
  }
  async findOne(filter:Document={},options:Document={}):Promise<Document|null>{return (await this.find(filter,{...options,limit:1}))[0]||null;}
  async findById(id:string):Promise<Document|null>{return (await this.store.list(this.name,id))[0]||null;}
  async countDocuments(filter:Document={}):Promise<number>{find([],filter,{});const count=await this.store.query(this.name,this.schema,filter,{},this.maxRows,this.maxBytes,true);return count===null?(await this.find(filter)).length:count as number;}
  async aggregate(pipeline:Value):Promise<Document[]> {
    validatePipeline(pipeline);
    const remaining=[...pipeline];const predicate=(remaining[0]&&Object.hasOwn(remaining[0],'$match')?remaining.shift()!.$match:{}) as Document;
    if(remaining.length===1&&Object.hasOwn(remaining[0],'$count')){const count=await this.countDocuments(predicate);return count?[{[String(remaining[0].$count)]:count}]:[];}
    const options:Document={};for(const [op,name]of [['$sort','sort'],['$skip','skip'],['$limit','limit']])if(remaining[0]&&Object.hasOwn(remaining[0],op))options[name]=remaining.shift()![op];
    let input:Document[]|null=null;if(!Object.keys(options).length&&remaining[0]&&Object.hasOwn(remaining[0],'$group'))input=await this.store.group(this.name,this.schema,predicate,remaining[0].$group as Document,this.maxRows,this.maxBytes);
    if(input!==null)remaining.shift();else input=await this.find(predicate,options);
    const rows=aggregate(input,remaining,this.maxRows,this.maxBytes);
    if(Buffer.byteLength(JSON.stringify(rows))>this.maxBytes)throw new QueryError('Query result byte limit exceeded','QUERY_LIMIT',413);
    return rows;
  }
  async create(input:Document):Promise<Document>{
    const data=this.valid(await this.beforeCreate(structuredClone(input)));
    const result:Document=await this.store.save(this.name,data);await this.afterCreate(structuredClone(result));return result;
  }
  /** Insert-only race protection plus CAS updates; caller retries are not deduplicated. */
  async upsertById(id:string,update:Document,onInsert:Document):Promise<{record:Document;inserted:boolean}>{
    if(typeof id!=='string'||!id||id.length>36)throw new QueryError('Invalid upsert id');
    updated({},update);const initial=this.valid(onInsert);
    for(let attempt=0;attempt<16;attempt++){
      const result=await this.mutate(id,current=>updated(current,update));if(result)return {record:result.row,inserted:false};
      const data=this.valid(await this.beforeCreate(updated(structuredClone(initial),update)));
      if(await this.store.insertIfAbsent(this.name,id,data)){const record={...data,id};await this.afterCreate(structuredClone(record));return {record,inserted:true};}
    }throw new QueryError('Concurrent upsert; retry the operation','WRITE_CONFLICT',409);
  }
  async insertMany(records:Document[]):Promise<Document[]>{
    if(!Array.isArray(records)||records.length>this.maxRows)throw new QueryError('Invalid bulk size');
    records.forEach(r=>this.valid(r));const result:Document[]=[];for(const record of records)result.push(await this.create(record));return result;
  }
  private async mutate(id:string,transform:(row:Document)=>Document,filter?:Document):Promise<{row:Document;modified:boolean}|null>{
    // Before hooks can be retried; use after hooks for post-commit side effects.
    for(let attempt=0;attempt<16;attempt++){
      const snapshot=await this.store.snapshot(this.name,id);if(!snapshot||filter&&!find([snapshot.row],filter).length)return null;
      const current=snapshot.row,data=this.valid(await this.beforeUpdate(structuredClone(current),transform(structuredClone(current))));
      const previous={...current};delete previous.id;delete previous._id;
      if(distinct([{value:previous},{value:data}],'value').length===1)return {row:current,modified:false};
      if(await this.store.compareWrite(this.name,id,snapshot.token,data)){const row={...data,id};await this.afterUpdate(structuredClone(row));return {row,modified:true};}
      await new Promise(resolve=>setTimeout(resolve,Math.random()*Math.min(50,2**attempt)));
    }throw new QueryError('Concurrent update; retry the operation','WRITE_CONFLICT',409);
  }
  async updateById(id:string,changes:Document):Promise<Document>{
    if(!changes||typeof changes!=='object'||Array.isArray(changes))throw new QueryError('Update must be an object');
    const result=await this.mutate(id,current=>({...current,...structuredClone(changes)}));if(!result)throw new QueryError('Not found','NOT_FOUND',404);return result.row;
  }
  private async deleteMatching(id:string,filter?:Document):Promise<boolean>{
    const snapshot=await this.store.snapshot(this.name,id);if(!snapshot||filter&&!find([snapshot.row],filter).length)return false;
    await this.beforeDelete(structuredClone(snapshot.row));if(!await this.store.compareWrite(this.name,id,snapshot.token,null))throw new QueryError('Concurrent update; retry the operation','WRITE_CONFLICT',409);
    await this.afterDelete(structuredClone(snapshot.row));return true;
  }
  async deleteById(id:string):Promise<{deleted:true}>{if(!await this.deleteMatching(id))throw new QueryError('Not found','NOT_FOUND',404);return {deleted:true};}
  async deleteOne(filter:Document={}):Promise<Document>{const row=await this.findOne(filter);return {acknowledged:true,deletedCount:row&&await this.deleteMatching(String(row.id),filter)?1:0};}
  async deleteMany(filter:Document={}):Promise<Document>{const rows=await this.find(filter);let deleted=0;for(const row of rows)if(await this.deleteMatching(String(row.id),filter))deleted++;return {acknowledged:true,deletedCount:deleted};}
  async distinct(path:string,filter:Document={}):Promise<Value[]>{distinct([],path);return distinct(await this.find(filter),path);}
  private async update(filter:Document,update:Document,many:boolean):Promise<Document>{
    updated({},update);
    const rows=await this.find(filter,many?{}:{limit:1});
    rows.forEach(row=>this.valid(updated(row,update)));let matched=0,modified=0;
    for(const row of rows){const result=await this.mutate(String(row.id),current=>updated(current,update),filter);if(result){matched++;if(result.modified)modified++;}}
    return {acknowledged:true,matchedCount:matched,modifiedCount:modified};
  }
  async updateOne(filter:Document,update:Document):Promise<Document>{return this.update(filter,update,false);}
  async updateMany(filter:Document,update:Document):Promise<Document>{return this.update(filter,update,true);}


  /** Server-only: authorize returns a target read filter, or null to deny. */
  async populate(rows:Document[],relation:string,target:Model,authorize:(name:string)=>Promise<Document|null>|Document|null):Promise<Document[]>{
    const spec=(this.schema['x-huza-relations'] as Record<string,{model:string;localField:string;foreignField:string;many:boolean}>|undefined)?.[relation];
    if(!spec||target.store!==this.store||target.name!==spec.model)throw new QueryError('Unknown relationship or target model');
    if(typeof authorize!=='function')throw new QueryError('Relationship read policy required','FORBIDDEN',403);
    const policy=await authorize(target.name);if(policy===null||policy===undefined)throw new QueryError('Related model access denied','FORBIDDEN',403);find([],policy,{});
    if(!Array.isArray(rows)||rows.length>this.maxRows||Buffer.byteLength(JSON.stringify(rows))>this.maxBytes)throw new QueryError('Relationship input limit exceeded','QUERY_LIMIT',413);
    const get=(row:Document,path:string):Value|undefined=>{let value:Value|undefined=row;for(const key of path.split('.')){if(!value||typeof value!=='object'||Array.isArray(value))return undefined;value=value[key];}return value;};
    const keys=new Map<string,Value>();for(const row of rows){const value=get(row,spec.localField);if(value!=null)keys.set(canonical(value),value);}
    const related=keys.size?await target.find({$and:[policy,{[spec.foreignField]:{$in:[...keys.values()]}}]}):[],groups=new Map<string,Document[]>();
    for(const row of related){const value=get(row,spec.foreignField);if(value!=null){const key=canonical(value);const members=groups.get(key)||[];members.push(row);groups.set(key,members);}}
    let bytes=0;return rows.map(source=>{const value=get(source,spec.localField),matches=value==null?[]:groups.get(canonical(value))||[];
      if(!spec.many&&matches.length>1)throw new QueryError('Singular relationship is ambiguous','RELATION_CONFLICT',409);
      const row={...source,[relation]:spec.many?matches:matches[0]||null};bytes+=Buffer.byteLength(JSON.stringify(row));if(bytes>this.maxBytes)throw new QueryError('Relationship result limit exceeded','QUERY_LIMIT',413);return structuredClone(row);
    });
  }
}
