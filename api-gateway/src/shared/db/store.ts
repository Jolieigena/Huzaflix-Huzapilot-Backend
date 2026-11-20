import {observe} from '../monitoring/telemetry';
import {ResponseCache} from '../cache';
import {plan,mongoPlan,groupPlan,candidateIds} from './planner';
import {find,QueryError,Document} from './query';
import {randomUUID} from 'crypto';
const env = process.env;
export class Store {
  readonly cache=new ResponseCache();
  kind = env.DB_TYPE || 'postgres';
  sql: any; mongo: any; cass: any; private mongoClient: any;
  table(module: string) { return 'hp_' + module.replace(/-/g, '_'); }
  async init(modules: string[]) {
    const poolSize=Math.min(32,Math.max(1,Number(env.DB_POOL_SIZE||4)));
    if (this.kind === 'mongodb') {
      const {MongoClient} = require('mongodb');
      const client = new MongoClient(`mongodb://${encodeURIComponent(env.DB_USER!)}:${encodeURIComponent(env.DB_PASSWORD!)}@${env.DB_HOST}:27017/?authSource=admin`,{maxPoolSize:poolSize,waitQueueTimeoutMS:10000,serverSelectionTimeoutMS:10000});
      await client.connect(); this.mongoClient = client; this.mongo = client.db(env.DB_NAME);
    } else if (this.kind === 'cassandra') {
      const {Client, auth} = require('cassandra-driver');
      this.cass = new Client({contactPoints: [env.DB_HOST], localDataCenter: 'datacenter1', authProvider: new auth.PlainTextAuthProvider(env.DB_USER, env.DB_PASSWORD)});
      await this.cass.connect();
      await this.cass.execute(`CREATE KEYSPACE IF NOT EXISTS ${env.DB_NAME} WITH replication = {'class':'SimpleStrategy','replication_factor':1}`);
      for (const m of modules) await this.cass.execute(`CREATE TABLE IF NOT EXISTS ${env.DB_NAME}.${this.table(m)} (id text PRIMARY KEY, data text)`);
    } else {
      this.sql = require('knex')({client: {postgres:'pg',mysql:'mysql2',sqlite:'better-sqlite3'}[this.kind], connection: this.kind === 'sqlite' ? {filename: '/data/app.sqlite'} : {host:env.DB_HOST, port: Number(env.DB_PORT), user:env.DB_USER, password:env.DB_PASSWORD, database:env.DB_NAME}, useNullAsDefault:true, pool:{min:1,max:this.kind==='sqlite'?1:poolSize},acquireConnectionTimeout:10000});
      if(this.kind==='sqlite'){await this.sql.raw('PRAGMA busy_timeout=10000');await this.sql.raw('PRAGMA journal_mode=WAL');}
      const identityType=this.kind==='mysql'?'varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin':this.kind==='postgres'?'varchar(36) COLLATE "C"':'varchar(36)';
      for (const m of modules) await this.ddl(this.table(m),`CREATE TABLE IF NOT EXISTS ${this.table(m)} (id ${identityType} PRIMARY KEY, data text NOT NULL)`);
    }
    await this.ensureIndexes(modules);
  }
  private async ddl(table:string,statement:string){
    if(this.kind!=='postgres'){await this.sql.raw(statement);return;}
    await this.sql.transaction(async (transaction:any)=>{await transaction.raw('SELECT pg_advisory_xact_lock(hashtext(?))',['huzapilot:'+table]);await transaction.raw(statement);});
  }
  private async ensureIndexes(modules:string[]){
    const fs=require('fs');if(!fs.existsSync('db-indexes.json'))return;
    const indexes=JSON.parse(fs.readFileSync('db-indexes.json','utf8')) as {module:string;name:string;fields:{path:string;direction:number}[];unique:boolean;partialFilter:Document;sql:string}[];
    for(const index of indexes){
      if(!modules.includes(index.module))continue;const table=this.table(index.module);
      if(this.mongo){await this.mongo.collection(table).createIndex(Object.fromEntries(index.fields.map(v=>[v.path,v.direction])),{name:index.name,unique:index.unique,collation:{locale:'simple'},...(index.unique?{partialFilterExpression:index.partialFilter}:{})});}
      else if(this.sql){
        if(this.kind==='mysql'&&(await this.sql('information_schema.statistics').whereRaw('table_schema=DATABASE()').where({table_name:table,index_name:index.name})).length)continue;
        try{await this.ddl(table,index.sql);}catch(error){if(this.kind!=='mysql'||(error as {errno?:number}).errno!==1061)throw error;}
      }
    }
  }
  async list(module: string, id?: string): Promise<any[]> {return observe(module,"list",()=>this.observed_list(module,id));}
  private async observed_list(module: string, id?: string): Promise<any[]> {
    const table = this.table(module);
    if (this.mongo) return (await this.mongo.collection(table).find(id ? {_id:id}:{}).toArray()).map(({_id,...d}:any)=>({...d,id:_id}));
    const rows = this.cass ? (await this.cass.execute(`SELECT id,data FROM ${env.DB_NAME}.${table}`+(id?' WHERE id=?':''), id?[id]:[], {prepare:true})).rows : await this.sql(table).select('*').modify((q:any)=>{if(id) q.where({id});});
    return rows.map((r:any)=>({...JSON.parse(r.data),id:r.id}));
  }
  private async saveRaw(module: string, data: any, id?: string) {
    const fresh=id===undefined;id=id??randomUUID();
    const table=this.table(module); const {_id, id:ignored, ...body}=data;
    if(this.mongo) await this.mongo.collection(table).replaceOne({_id:id},body,{upsert:true});
    else if(this.cass) await this.cass.execute(`INSERT INTO ${env.DB_NAME}.${table} (id,data) VALUES (?,?)`,[id,JSON.stringify(body)],{prepare:true});
    else if(fresh)await this.sql(table).insert({id,data:JSON.stringify(body)});
    else await this.sql(table).where({id}).update({data:JSON.stringify(body)});
    return {...body,id};
  }
  private async removeRaw(module:string,id:string) {
    const table=this.table(module);
    if(this.mongo) await this.mongo.collection(table).deleteOne({_id:id});
    else if(this.cass) await this.cass.execute(`DELETE FROM ${env.DB_NAME}.${table} WHERE id=?`,[id],{prepare:true});
    else await this.sql(table).where({id}).delete();
  }
  async scan(module: string, maxRows: number, maxBytes: number): Promise<Document[]> {return observe(module,"scan",()=>this.observed_scan(module,maxRows,maxBytes));}
  private async observed_scan(module: string, maxRows: number, maxBytes: number): Promise<Document[]> {
    const rows: Document[] = []; let bytes = 0;
    const append = (row: Document, size: number) => {
      bytes += size;
      if (rows.length >= maxRows || bytes > maxBytes) throw new QueryError('Query scan limit exceeded; narrow the data or use an explicit native query', 'QUERY_LIMIT', 413);
      rows.push(row);
    };
    const table = this.table(module);
    if (this.mongo) {
      const cursor = this.mongo.collection(table).find({}).limit(maxRows + 1).batchSize(64);
      try { for await (const record of cursor) { const {_id, ...data} = record; append({...data,id:_id},Buffer.byteLength(JSON.stringify(record))); } }
      finally { await cursor.close(); }
    } else if (this.cass) {
      let pageState: string | undefined;
      do {
        const result = await this.cass.execute(`SELECT id,data FROM ${env.DB_NAME}.${table} LIMIT ${maxRows + 1}`,[],{prepare:true,fetchSize:64,pageState});
        for (const record of result.rows) append({...JSON.parse(record.data),id:record.id},Buffer.byteLength(record.data));
        pageState = result.pageState;
      } while (pageState);
    } else {
      // Small bounded pages avoid a whole-result allocation in SQLite's driver.
      for (let offset=0; offset<=maxRows; offset+=64) {
        const batch = await this.sql(table).select('id','data').orderBy('id').limit(Math.min(64,maxRows+1-offset)).offset(offset);
        for (const record of batch) append({...JSON.parse(record.data),id:record.id},Buffer.byteLength(record.data));
        if (batch.length<64) break;
      }
    }
    return rows;
  }
  async query(module:string,schema:Record<string,unknown>,filter:Document,options:Document,maxRows:number,maxBytes:number,count=false):Promise<Document[]|number|null>{return observe(module,"query",()=>this.observed_query(module,schema,filter,options,maxRows,maxBytes,count));}
  private async observed_query(module:string,schema:Record<string,unknown>,filter:Document,options:Document,maxRows:number,maxBytes:number,count=false):Promise<Document[]|number|null>{
    if(this.cass){
      const ids=candidateIds(filter);if(ids===null)return null;if(ids.length>maxRows)throw new QueryError('Query partition limit exceeded','QUERY_LIMIT',413);
      const rows:Document[]=[];let bytes=0;for(const id of ids)for(const row of await this.list(module,id)){bytes+=Buffer.byteLength(JSON.stringify(row));if(bytes>maxBytes)throw new QueryError('Query result byte limit exceeded','QUERY_LIMIT',413);rows.push(row);}
      return count?find(rows,filter,{}).length:find(rows,filter,options);
    }
    if(this.mongo){
      const compiled=mongoPlan(schema,filter,options);if(!compiled)return null;const collection=this.mongo.collection(this.table(module));
      if(count)return collection.countDocuments(compiled);
      const sort=Object.fromEntries(Object.entries((options.sort||{}) as Document).map(([k,v])=>[k==='id'?'_id':k,v]));if(!Object.hasOwn(sort,'_id'))sort._id=1;
      const fetch=typeof options.limit==='number'&&options.limit<=maxRows?options.limit:maxRows+1;
      const cursor=collection.find(compiled).sort(sort).skip(Number(options.skip||0)).limit(fetch).batchSize(64);const rows:Document[]=[];let bytes=0;
      try{for await(const record of cursor){const {_id,...body}=record;bytes+=Buffer.byteLength(JSON.stringify(record));if(rows.length>=maxRows||bytes>maxBytes)throw new QueryError('Query result limit exceeded','QUERY_LIMIT',413);rows.push({...body,id:_id});}}finally{await cursor.close();}
      return find(rows,{},Object.hasOwn(options,'projection')?{projection:options.projection}:{});
    }
    const compiled=plan(this.kind,schema,filter,options);if(!compiled)return null;
    const query=this.sql(this.table(module)).whereRaw(compiled.where,compiled.params);
    if(count){const row=await query.count({total:'*'}).first();return Number(row.total);}
    const fetch=typeof options.limit==='number'&&options.limit<=maxRows?options.limit:maxRows+1;
    let bytes=0;const rows:Document[]=[];
    for(let offset=0;offset<fetch;offset+=64){
      const records=await query.clone().select('id','data').orderByRaw(compiled.order).limit(Math.min(64,fetch-offset)).offset(Number(options.skip||0)+offset);
      for(const record of records){bytes+=Buffer.byteLength(record.data);if(rows.length>=maxRows||bytes>maxBytes)throw new QueryError('Query result limit exceeded','QUERY_LIMIT',413);rows.push({...JSON.parse(record.data),id:record.id});}
      if(records.length<64)break;
    }
    return find(rows,{},Object.hasOwn(options,'projection')?{projection:options.projection}:{});
  }
  async group(module:string,schema:Record<string,unknown>,predicate:Document,group:Document,maxRows:number,maxBytes:number):Promise<Document[]|null>{return observe(module,"group",()=>this.observed_group(module,schema,predicate,group,maxRows,maxBytes));}
  private async observed_group(module:string,schema:Record<string,unknown>,predicate:Document,group:Document,maxRows:number,maxBytes:number):Promise<Document[]|null>{
    const compiled=groupPlan(this.kind,schema,predicate,group);if(!compiled)return null;const rows:Document[]=[];let bytes=0;
    const append=(record:Record<string,unknown>)=>{
      if(!Number(record.hp_count))return;
      let key=compiled.constant?compiled.key:record[this.mongo?'_id':'hp_key'];
      if(!compiled.constant&&key!=null){if(['integer','number'].includes(compiled.keyType))key=Number(key);else if(compiled.keyType==='boolean')key=Boolean(key);else if(Buffer.isBuffer(key))key=key.toString('utf8');}
      const row:Document={_id:key as Document[string]};
      for(const column of compiled.columns){const value=column.nullAlias&&Number(record[column.nullAlias])?null:record[column.alias];row[column.name]=value==null?null:Number(value);}
      bytes+=Buffer.byteLength(JSON.stringify(row));if(rows.length>=maxRows||bytes>maxBytes)throw new QueryError('Query result limit exceeded','QUERY_LIMIT',413);rows.push(row);
    };
    if(this.mongo){const cursor=this.mongo.collection(this.table(module)).aggregate([...compiled.mongo,{$limit:maxRows+1}],{batchSize:64,allowDiskUse:false,maxTimeMS:15000});try{for await(const row of cursor)append(row);}finally{await cursor.close();}}
    else{
      const predicatePlan=plan(this.kind,schema,predicate,{})!;
      const query=this.sql(this.table(module)).select(this.sql.raw(compiled.select,compiled.params.slice(0,compiled.params.length-predicatePlan.params.length))).whereRaw(compiled.where,predicatePlan.params);
      if(compiled.groupBy)query.groupByRaw(compiled.groupBy);
      if(this.kind==='sqlite'){for(let offset=0;offset<=maxRows;offset+=64){const records=await query.clone().orderByRaw('hp_key ASC').limit(Math.min(64,maxRows+1-offset)).offset(offset);for(const row of records)append(row);if(records.length<64)break;}}
      else{const stream=query.limit(maxRows+1).stream({highWaterMark:64});try{for await(const row of stream)append(row);}finally{stream.destroy();}}
    }return rows;
  }
  async snapshot(module:string,id:string):Promise<{row:Document;token:unknown}|null>{return observe(module,"snapshot",()=>this.observed_snapshot(module,id));}
  private async observed_snapshot(module:string,id:string):Promise<{row:Document;token:unknown}|null>{
    const table=this.table(module);
    if(this.mongo){const token=await this.mongo.collection(table).findOne({_id:id});if(!token)return null;const {_id,...body}=token;return {row:{...body,id},token};}
    const record=this.cass?(await this.cass.execute(`SELECT data FROM ${env.DB_NAME}.${table} WHERE id=?`,[id],{prepare:true})).rows[0]:await this.sql(table).select('data').where({id}).first();
    return record?{row:{...JSON.parse(record.data),id},token:record.data}:null;
  }
  private async compareWriteRaw(module:string,id:string,token:unknown,data:Document|null):Promise<boolean>{
    const table=this.table(module),body=data===null?null:{...data};if(body){delete body.id;delete body._id;}
    if(this.mongo){const predicate={_id:id,$expr:{$eq:['$$ROOT',{$literal:token}]}};return body===null?(await this.mongo.collection(table).deleteOne(predicate)).deletedCount===1:(await this.mongo.collection(table).replaceOne(predicate,body)).matchedCount===1;}
    if(this.cass){const result=await this.cass.execute(body===null?`DELETE FROM ${env.DB_NAME}.${table} WHERE id=? IF data=?`:`UPDATE ${env.DB_NAME}.${table} SET data=? WHERE id=? IF data=?`,body===null?[id,token]:[JSON.stringify(body),id,token],{prepare:true});return result.rows[0]['[applied]']===true;}
    const query=this.sql(table).where({id}).whereRaw(this.kind==='mysql'?'CAST(data AS BINARY)=CAST(? AS BINARY)':'data=?',[token]);
    return (body===null?await query.delete():await query.update({data:JSON.stringify(body)}))===1;
  }
  private writeError(error:unknown):unknown{
    const driver=error as {code?:string|number;errno?:number};
    if(driver?.code===11000||driver?.code==='23505'||driver?.code==='ER_DUP_ENTRY'||driver?.errno===1062||['SQLITE_CONSTRAINT_UNIQUE','SQLITE_CONSTRAINT_PRIMARYKEY'].includes(String(driver?.code)))return new QueryError('Unique constraint violation','UNIQUE_CONFLICT',409);
    return error;
  }
  async save(module:string,data:Record<string,unknown>,id?:string){return observe(module,"save",()=>this.observed_save(module,data,id));}
  private async observed_save(module:string,data:Record<string,unknown>,id?:string){try{const row=await this.saveRaw(module,data,id);await this.cache.invalidate(module);return row;}catch(error){throw this.writeError(error);}}
  async compareWrite(module:string,id:string,token:unknown,data:Document|null):Promise<boolean>{return observe(module,"compareWrite",()=>this.observed_compareWrite(module,id,token,data));}
  private async observed_compareWrite(module:string,id:string,token:unknown,data:Document|null):Promise<boolean>{try{const applied=await this.compareWriteRaw(module,id,token,data);if(applied)await this.cache.invalidate(module);return applied;}catch(error){throw this.writeError(error);}}
  private async insertIfAbsentRaw(module:string,id:string,input:Document):Promise<boolean>{
    const body={...input};delete body.id;delete body._id;const table=this.table(module);
    try{
      if(this.mongo)await this.mongo.collection(table).insertOne({...body,_id:id});
      else if(this.cass)return (await this.cass.execute(`INSERT INTO ${env.DB_NAME}.${table} (id,data) VALUES (?,?) IF NOT EXISTS`,[id,JSON.stringify(body)],{prepare:true})).rows[0]['[applied]']===true;
      else await this.sql(table).insert({id,data:JSON.stringify(body)});
      return true;
    }catch(error){const conflict=this.writeError(error);if(conflict instanceof QueryError&&conflict.code==='UNIQUE_CONFLICT'&&await this.snapshot(module,id))return false;throw conflict;}
  }
  async remove(module:string,id:string){return observe(module,"remove",()=>this.observed_remove(module,id));}
  private async observed_remove(module:string,id:string){const result=await this.removeRaw(module,id);await this.cache.invalidate(module);return result;}
  async insertIfAbsent(module:string,id:string,input:Document){return observe(module,"insertIfAbsent",()=>this.observed_insertIfAbsent(module,id,input));}
  private async observed_insertIfAbsent(module:string,id:string,input:Document){const applied=await this.insertIfAbsentRaw(module,id,input);if(applied)await this.cache.invalidate(module);return applied;}
  native() { return this.sql || this.mongo || this.cass; }
  onModuleDestroy() { return this.close(); }
  async close() { this.cache.close(); if(this.sql) await this.sql.destroy(); if(this.mongoClient) await this.mongoClient.close(); if(this.cass) await this.cass.shutdown(); }

}
