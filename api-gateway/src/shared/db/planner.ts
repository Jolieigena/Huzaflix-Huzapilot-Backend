/** Parameterized SQL planning; unsupported semantics use the bounded evaluator. */
import {Document,Value} from './query';
export interface Plan {where:string;params:Value[];order:string}
class Unsupported extends Error {}
export function scalarColumn(kind:string,schema:Record<string,unknown>,path:string):[string,string,string]{
    if(path==='id')return ['id',"'string'",'string'];
    let node=schema;const parts=path.split('.');
    for(const part of parts){if(!/^[A-Za-z_][A-Za-z0-9_-]*$/.test(part))throw new Unsupported();node=(node.properties as Record<string,Record<string,unknown>>|undefined)?.[part]||{};if(node.type==='array')throw new Unsupported();}
    const types=Array.isArray(node.type)?node.type.filter(t=>t!=='null'):[node.type];const typ=types.length===1?String(types[0]):'';if(!['string','integer','number','boolean'].includes(typ))throw new Unsupported();
    let value:string,tag:string;
    if(kind==='postgres'){
      const raw=`data::jsonb #> '{${parts.join(',')}}'`,scalar=`data::jsonb #>> '{${parts.join(',')}}'`;
      tag=`jsonb_typeof(${raw})`;value=typ==='string'?`(${scalar}) COLLATE "C"`:`CAST(${scalar} AS ${typ==='boolean'?'boolean':'numeric'})`;
    }else{
      const pathSql='$.'+parts.map(p=>'"'+p+'"').join('.'),raw=`json_extract(data, '${pathSql}')`;
      tag=kind==='sqlite'?`json_type(data, '${pathSql}')`:`LOWER(JSON_TYPE(${raw}))`;
      value=kind==='sqlite'?raw:typ==='string'?`CASE WHEN ${tag}='null' THEN NULL ELSE JSON_UNQUOTE(${raw}) END`:`NULLIF(JSON_UNQUOTE(${raw}), 'null')`;
      if(typ==='string'){const length=node.maxLength;value=kind==='sqlite'?`(${value}) COLLATE BINARY`:typeof length==='number'&&Number.isInteger(length)&&length>=1&&length<=191?`CAST((${value}) AS CHAR(${length}) CHARACTER SET utf8mb4) COLLATE utf8mb4_0900_bin`:`CAST((${value}) AS BINARY)`;}
      else if(kind==='mysql')value=typ==='boolean'?`CASE WHEN ${tag} IS NULL OR ${tag}='null' THEN NULL ELSE (${raw}=true) END`:`CAST(${value} AS DOUBLE)`;
    }return [value,tag,typ];
  }

export function plan(kind:string,schema:Record<string,unknown>,query:Document={},options:Document={}):Plan|null {
  if(!['postgres','mysql','sqlite'].includes(kind))return null;
  const params:Value[]=[];
  const bind=(value:Value)=>{params.push(value);return '?';};
  const column=(path:string)=>scalarColumn(kind,schema,path);
  const join=(parts:string[],op:string)=>parts.length?'('+parts.join(' '+op+' ')+')':op==='AND'?'1=1':'1=0';
  const clause=(path:string,condition:Value):string=>{
    const [value,tag,typ]=column(path),ops=condition&&typeof condition==='object'&&!Array.isArray(condition)&&Object.keys(condition).some(k=>k.startsWith('$'))?condition:{$eq:condition};const out:string[]=[];
    for(const [op,arg]of Object.entries(ops)){
      if(op==='$exists'){out.push(`${tag} IS ${arg?'NOT ':''}NULL`);continue;}
      if(op==='$not'){out.push('NOT '+clause(path,arg));continue;}
      if(op==='$in'||op==='$nin'){out.push((op==='$nin'?'NOT ':'')+join((arg as Value[]).map(v=>clause(path,{$eq:v})),'OR'));continue;}
      if(!['$eq','$ne','$gt','$gte','$lt','$lte'].includes(op))throw new Unsupported();
      let sql:string;
      if(arg===null)sql=['$eq','$ne'].includes(op)?`(${tag} IS NULL OR ${tag}='null')`:'1=0';
      else{
        const compatible=typeof arg===(typ==='string'?'string':typ==='boolean'?'boolean':'number');
        if(!compatible||typ==='boolean'&&!['$eq','$ne'].includes(op))sql='1=0';
        else{const operator:Record<string,string>={$eq:'=',$ne:'=',$gt:'>',$gte:'>=',$lt:'<',$lte:'<='};sql=`(${value} IS NOT NULL AND ${value} ${operator[op]} ${bind(arg)})`;}
      }out.push(op==='$ne'?'NOT ('+sql+')':'('+sql+')');
    }return join(out,'AND');
  };
  const walk=(filter:Document):string=>join(Object.entries(filter).map(([key,value])=>{
    if(['$and','$or','$nor'].includes(key)){const sql=join((value as Document[]).map(walk),key==='$and'?'AND':'OR');return key==='$nor'?'NOT '+sql:sql;}
    if(key.startsWith('$'))throw new Unsupported();return clause(key,value);
  }),'AND');
  try{
    const where=walk(query),order:string[]=[],sort=(options.sort||{}) as Document;
    for(const [path,direction]of Object.entries(sort)){const [value]=column(path),dir=direction===1?'ASC':'DESC';order.push(`(${value} IS NOT NULL) ${dir}`,`${value} ${dir}`);}
    if(!Object.hasOwn(sort,'id'))order.push('id ASC');return {where,params,order:order.join(', ')};
  }catch(error){if(error instanceof Unsupported)return null;throw error;}
}

export function mongoPlan(schema:Record<string,unknown>,query:Document,options:Document):Document|null{
  if(!plan('postgres',schema,query,options))return null;
  const join=(items:Document[],op:string):Document=>items.length?{[op]:items}:op==='$and'?{}:{$expr:false};
  const clause=(path:string,condition:Value):Document=>{
    if(path==='id')path='_id';
    const ops=condition&&typeof condition==='object'&&!Array.isArray(condition)&&Object.keys(condition).some(k=>k.startsWith('$'))?condition:{$eq:condition};
    return join(Object.entries(ops).map(([op,arg]):Document=>{
      if(op==='$not')return {$nor:[clause(path,arg)]};
      if(op==='$in'||op==='$nin'){const expr=join((arg as Value[]).map(v=>clause(path,{$eq:v})),'$or');return op==='$nin'?{$nor:[expr]}:expr;}
      if(op==='$ne')return {$nor:[{[path]:{$eq:arg}}]};
      if(['$gt','$gte','$lt','$lte'].includes(op)&&!['string','number'].includes(typeof arg))return {$expr:false};
      return {[path]:{[op]:arg}};
    }),'$and');
  };
  const walk=(filter:Document):Document=>join(Object.entries(filter).map(([key,value]):Document=>{
    if(['$and','$or','$nor'].includes(key))return join((value as Document[]).map(walk),key);
    return clause(key,value);
  }),'$and');return walk(query);
}

export interface GroupPlan extends Plan {select:string;groupBy:string|null;key:Value;constant:boolean;keyType:string;columns:{name:string;alias:string;nullAlias:string|null}[];mongo:Document[]}
export function groupPlan(kind:string,schema:Record<string,unknown>,query:Document,group:Document):GroupPlan|null{
  if(kind==='cassandra')return null;
  const compiled=plan(kind==='mongodb'?'postgres':kind,schema,query,{});if(!compiled)return null;
  const params:Value[]=[],columns:GroupPlan['columns']=[],mongo:Document={hp_count:{$sum:1}},key=group._id;let constant=true,keyType='literal',keySql='NULL';
  try{
    if(typeof key==='string'&&key.startsWith('$')){constant=false;[keySql,,keyType]=scalarColumn(kind==='mongodb'?'postgres':kind,schema,key.slice(1));mongo._id={$ifNull:[key==='$id'?'$_id':key,null]};}
    else if(key===null||['string','number','boolean'].includes(typeof key))mongo._id={$literal:key};else return null;
    const select=[keySql+' AS hp_key','COUNT(*) AS hp_count'];
    for(const [name,spec]of Object.entries(group)){
      if(name==='_id')continue;const [op,arg]=Object.entries(spec as Document)[0];if(!['$sum','$avg','$min','$max'].includes(op))return null;
      const alias='hp_a'+columns.length;let numeric=false,term='CAST(NULL AS DECIMAL)',expression:Value={$literal:arg};
      if(typeof arg==='string'&&arg.startsWith('$')){const [value,,typ]=scalarColumn(kind==='mongodb'?'postgres':kind,schema,arg.slice(1));numeric=['integer','number'].includes(typ);if(numeric)term=value;expression=arg==='$id'?'$_id':arg;}
      else if(typeof arg==='number'){numeric=true;term='CAST(? AS '+(kind==='mysql'?'DOUBLE':'DOUBLE PRECISION')+')';params.push(arg);}else if(arg!==null&&typeof arg==='object')return null;
      if(['$min','$max'].includes(op)&&!numeric)return null;
      let sql=op.slice(1).toUpperCase()+'('+term+')';if(op==='$sum')sql='COALESCE('+sql+',0)';const nullAlias=op==='$min'?'hp_n'+columns.length:null;
      if(nullAlias){select.push('COUNT(*)-COUNT('+term+') AS '+nullAlias);if(term.startsWith('CAST(?'))params.push(arg);mongo[nullAlias]={$sum:{$cond:[{$eq:[{$ifNull:[expression,null]},null]},1,0]}};}
      select.push(sql+' AS '+alias);mongo[alias]={[op]:expression};columns.push({name,alias,nullAlias});
    }
    return {...compiled,select:select.join(', '),params:[...params,...compiled.params],groupBy:constant?null:keySql,key,constant,keyType,columns,mongo:[{$match:mongoPlan(schema,query,{})!},{$group:mongo}]};
  }catch(error){if(error instanceof Unsupported)return null;throw error;}
}

export function candidateIds(predicate:Document):string[]|null{
 const candidates:string[][]=[];const value=predicate.id;
 if(typeof value==='string')candidates.push([value]);
 else if(value&&typeof value==='object'&&!Array.isArray(value)){
  if(typeof value.$eq==='string')candidates.push([value.$eq]);
  if(Array.isArray(value.$in)&&value.$in.every(v=>typeof v==='string'))candidates.push([...new Set(value.$in as string[])]);
 }
 for(const child of (predicate.$and||[]) as Document[]){const values=candidateIds(child);if(values!==null)candidates.push(values);}
 return candidates.length?candidates[0].filter(v=>candidates.every(c=>c.includes(v))):null;
}
export function candidateFilter(kind:string,schema:Record<string,unknown>,predicate:Document):Document{
 const clauses:Document[]=[];for(const [key,value]of Object.entries(predicate)){
  if(key==='$and'){for(const child of value as Document[]){const candidate=candidateFilter(kind,schema,child);if(Object.keys(candidate).length)clauses.push(candidate);}}
  else if(plan(kind==='mongodb'?'postgres':kind,schema,{[key]:value},{}))clauses.push({[key]:value});
 }return clauses.length?{$and:clauses}:{};
}
