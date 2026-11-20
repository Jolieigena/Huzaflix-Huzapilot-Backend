/** Portable document query contract v1. Bounded snapshots; no eval or raw SQL. */
export type Value = null | boolean | number | string | Value[] | Document;
export type Document = {[key: string]: Value};
export class QueryError extends Error {
  constructor(message: string, public code = 'INVALID_QUERY', public status = 400) { super(message); }
}
const MAX_ROWS = 10000, MAX_STAGES = 32;
const forbidden = new Set(['__proto__', 'prototype', 'constructor']);
const filters = new Set(['$eq','$ne','$gt','$gte','$lt','$lte','$in','$nin','$exists','$not','$all','$size','$elemMatch','$contains','$startsWith']);
const expressions = new Set(['$literal','$ifNull','$add','$subtract','$multiply','$divide','$concat','$toLower','$toUpper','$cond','$eq','$ne','$gt','$gte','$lt','$lte','$and','$or','$not','$in','$size','$arrayElemAt','$concatArrays','$dateToString']);
const accumulators = new Set(['$sum','$avg','$min','$max','$first','$last','$push','$addToSet']);
function requireValue(condition: unknown, message: string): asserts condition { if (!condition) throw new QueryError(message); }
function object(v: unknown): v is Document { return typeof v === 'object' && v !== null && !Array.isArray(v); }
function doc(v: unknown): Document { requireValue(object(v), 'Expected an object'); return v; }
function arr(v: unknown): Value[] { requireValue(Array.isArray(v), 'Expected an array'); return v; }
function text(v: unknown): string { requireValue(typeof v === 'string', 'Expected a string'); return v; }
function numeric(v: unknown): v is number { return typeof v === 'number' && Number.isFinite(v); }
const clone = <T>(v: T): T => structuredClone(v);
export function canonical(v: Value): string {
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']';
  if (object(v)) return '{' + Object.keys(v).sort().map(k => JSON.stringify(k)+':'+canonical(v[k])).join(',') + '}';
  return JSON.stringify(v);
}
function equal(a: Value | undefined, b: Value | undefined): boolean {
  return a === undefined || b === undefined ? a === b : canonical(a) === canonical(b);
}
function field(value: unknown): string {
  const path = text(value);
  requireValue(path.length > 0 && path.length <= 256 && path.split('.').every(p => /^[A-Za-z_][A-Za-z0-9_-]*$/.test(p) && !forbidden.has(p)), 'Invalid field path: '+path);
  return path;
}
function get(row: Value, path: string): Value | undefined {
  const parts=path.split('.');
  const walk=(value:Value,index:number):Value|undefined=>{
    if(index===parts.length)return value;
    if(Array.isArray(value)){const found=value.map(v=>walk(v,index)).filter((v):v is Value=>v!==undefined);return found.length?found.flatMap(v=>Array.isArray(v)?v:[v]):undefined;}
    return object(value)&&Object.hasOwn(value,parts[index])?walk(value[parts[index]],index+1):undefined;
  };return walk(row,0);
}
function scalarCondition(value:Value):boolean{return object(value)&&Object.keys(value).some(k=>k.startsWith('$')&&!['$and','$or','$nor'].includes(k));}
function elementMatches(item:Value,condition:Value):boolean{return scalarCondition(condition)?matches({value:item},{value:condition}):object(condition)?object(item)&&matches(item,condition):equal(item,condition);}
function put(row: Document, path: string, value: Value | undefined) {
  const parts = field(path).split('.'); let target = row;
  for (const part of parts.slice(0,-1)) { requireValue(!Array.isArray(target[part]),'Array path writes require explicit array replacement'); if (!object(target[part])) target[part] = {}; target = doc(target[part]); }
  const last = parts.at(-1)!;
  if (value === undefined) delete target[last]; else target[last] = clone(value);
}
function selection(value:Value,parts:string[]):Value{
  if(!parts.length)return clone(value);
  if(Array.isArray(value)){requireValue(value.every(item=>object(item)||Array.isArray(item)),'Nested projection requires an array of documents');return value.map(item=>selection(item,parts));}
  if(!object(value)||!Object.hasOwn(value,parts[0]))return {};
  return {[parts[0]]:selection(value[parts[0]],parts.slice(1))};
}
function mergeProjection(target:Value|undefined,source:Value):Value{
  if(object(target)&&object(source)){for(const [key,value]of Object.entries(source))target[key]=mergeProjection(target[key],value);return target;}
  if(Array.isArray(target)&&Array.isArray(source)&&target.length===source.length)return target.map((item,index)=>mergeProjection(item,source[index]));
  return clone(source);
}
function removeProjection(value:Value,parts:string[]):void{
  if(Array.isArray(value))value.forEach(item=>removeProjection(item,parts));
  else if(object(value)){if(parts.length===1)delete value[parts[0]];else if(Object.hasOwn(value,parts[0]))removeProjection(value[parts[0]],parts.slice(1));}
}
function compare(a: Value | undefined, b: Value | undefined): number {
  const rank = (v: Value | undefined): number => v == null ? 0 : typeof v === 'boolean' ? 1 : numeric(v) ? 2 : typeof v === 'string' ? 3 : Array.isArray(v) ? 4 : 5;
  const ra=rank(a), rb=rank(b); if (ra!==rb) return ra-rb; if (!ra) return 0;
  const av = ra>=4 ? canonical(a!) : a!, bv = rb>=4 ? canonical(b!) : b!;
  if (typeof av==='number' && typeof bv==='number') return av<bv?-1:av>bv?1:0;
  if (typeof av==='boolean' && typeof bv==='boolean') return Number(av)-Number(bv);
  return String(av)<String(bv)?-1:String(av)>String(bv)?1:0;
}
function validateValue(value: unknown, depth=0) {
  requireValue(depth<=32, 'Query nesting exceeds 32 levels');
  if (object(value)) for (const [key,child] of Object.entries(value)) { requireValue(!forbidden.has(key),'Unsafe object key'); validateValue(child,depth+1); }
  else if (Array.isArray(value)) { requireValue(value.length<=MAX_ROWS,'Array exceeds row limit'); for(const child of value) validateValue(child,depth+1); }
  else if (typeof value==='number') requireValue(numeric(value),'Non-finite numbers are unsupported');
}
function validateFilter(query: Value) {
  for (const [key,value] of Object.entries(doc(query))) {
    if(key==='$expr')validateExpression(value);
    else if (['$and','$or','$nor'].includes(key)) { const values=arr(value);requireValue(values.length>0,key+' needs a nonempty array');values.forEach(validateFilter); }
    else {
      field(key);
      if (object(value) && Object.keys(value).some(k=>k.startsWith('$'))) for(const [op,arg] of Object.entries(value)) {
        requireValue(filters.has(op),'Unsupported filter operator: '+op);
        if (['$in','$nin','$all'].includes(op)) arr(arg);
        if (op==='$exists') requireValue(typeof arg==='boolean','$exists needs a boolean');
        if (op==='$size') requireValue(numeric(arg)&&Number.isInteger(arg)&&arg>=0,'$size needs a nonnegative integer');
        if (op==='$not') validateFilter({[key]:arg});
        if (op==='$elemMatch') validateFilter(scalarCondition(arg)?{value:arg}:arg);
        if(op==='$contains'||op==='$startsWith')searchConfig(arg);
      }
    }
  }
}
function searchConfig(value:Value):[string,boolean]{
  if(typeof value==='string'){requireValue(value.length<=1024,'Search text exceeds 1024 characters');return [value,false];}
  const config=doc(value);requireValue(Object.keys(config).every(k=>['value','caseInsensitive'].includes(k))&&typeof config.value==='string'&&config.value.length<=1024&&(!Object.hasOwn(config,'caseInsensitive')||typeof config.caseInsensitive==='boolean'),'Invalid literal search');return [text(config.value),config.caseInsensitive===true];
}
function matches(row: Document, query: Document): boolean {
  for(const [key,condition] of Object.entries(query)) {
    if(key==='$expr'){if(!truth(expression(row,condition)))return false;continue;}
    if (['$and','$or','$nor'].includes(key)) {
      const results=arr(condition).map(q=>matches(row,doc(q)));
      if(key==='$and'&&!results.every(Boolean)||key==='$or'&&!results.some(Boolean)||key==='$nor'&&results.some(Boolean)) return false;
      continue;
    }
    const actual=get(row,key);
    const eq=(v:Value)=>v===null&&actual===undefined||equal(actual,v)||Array.isArray(actual)&&!Array.isArray(v)&&actual.some(a=>equal(a,v));
    if(!object(condition)||!Object.keys(condition).some(k=>k.startsWith('$'))) {if(!eq(condition))return false;continue;}
    for(const [op,v] of Object.entries(condition)) {
      if(op==='$eq'&&!eq(v)||op==='$ne'&&eq(v))return false;
      if(['$gt','$gte','$lt','$lte'].includes(op)) {
        const qualifies=(item:Value|undefined)=>{if(!(numeric(item)&&numeric(v)||typeof item==='string'&&typeof v==='string'))return false;const c=compare(item,v);return ({'$gt':c>0,'$gte':c>=0,'$lt':c<0,'$lte':c<=0} as Record<string,boolean>)[op];};
        if(!(Array.isArray(actual)?actual:[actual]).some(qualifies))return false;
      }
      if(op==='$in'&&!arr(v).some(eq)||op==='$nin'&&arr(v).some(eq))return false;
      if(op==='$exists'&&(actual!==undefined)!==v)return false;
      if(op==='$not'&&matches(row,{[key]:v}))return false;
      if(op==='$size'&&(!Array.isArray(actual)||actual.length!==v))return false;
      if(op==='$all'&&(!Array.isArray(actual)||!arr(v).every(b=>actual.some(a=>equal(a,b)))))return false;
      if(op==='$elemMatch'&&(!Array.isArray(actual)||!actual.some(a=>elementMatches(a,v))))return false;
      if(op==='$contains'||op==='$startsWith'){
        const [raw,insensitive]=searchConfig(v),needle=insensitive?raw.toLowerCase():raw;
        if(!(Array.isArray(actual)?actual:[actual]).some(item=>{if(typeof item!=='string')return false;const value=insensitive?item.toLowerCase():item;return op==='$contains'?value.includes(needle):value.startsWith(needle);}))return false;
      }
    }
  }
  return true;
}
function validateExpression(value: Value) {
  if(typeof value==='string'&&value.startsWith('$'))field(value.slice(1));
  else if(Array.isArray(value))value.forEach(validateExpression);
  else if(object(value)) {
    const ops=Object.keys(value).filter(k=>k.startsWith('$'));
    if(ops.length) {
      requireValue(Object.keys(value).length===1&&expressions.has(ops[0]),'Unsupported expression');
      const op=ops[0],arg=value[op];if(op==='$literal')return;
      if(['$eq','$ne','$gt','$gte','$lt','$lte','$in','$arrayElemAt'].includes(op))requireValue(Array.isArray(arg)&&arg.length===2,op+' needs two arguments');
      if(op==='$cond')requireValue(Array.isArray(arg)&&arg.length===3,'$cond needs [condition, then, else]');
      if(op==='$not')requireValue(Array.isArray(arg)&&arg.length===1,'$not needs one argument');
      if(['$and','$or','$concatArrays'].includes(op))arr(arg);
      if(op==='$dateToString')requireValue(object(arg)&&Object.hasOwn(arg,'date')&&Object.keys(arg).every(k=>['date','format','timezone','onNull'].includes(k)),'Invalid date expression');
      if(['$ifNull','$subtract','$divide'].includes(op))requireValue(Array.isArray(arg)&&arg.length===2,op+' needs two arguments');
      if(['$add','$multiply','$concat'].includes(op))requireValue(Array.isArray(arg)&&arg.length>0,op+' needs an argument array');
      validateExpression(arg);
    } else for(const [k,v]of Object.entries(value)){field(k);validateExpression(v);}
  }
}
function truth(value:Value):boolean{return value!==null&&value!==false&&value!==0;}
function dateString(arg:Document):Value{
  if(arg.date==null)return arg.onNull??null;
  const raw=text(arg.date);requireValue(/^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2}))?$/.test(raw),'Invalid ISO date');
  const calendar=new Date(raw.slice(0,10)+'T00:00:00Z');requireValue(!raw.startsWith('0000')&&Number.isFinite(calendar.getTime())&&calendar.toISOString().slice(0,10)===raw.slice(0,10)&&(raw.length===10||Number(raw.slice(11,13))<24),'Invalid ISO date');
  const date=new Date(raw);requireValue(Number.isFinite(date.getTime()),'Invalid ISO date');
  const zone=text(arg.timezone??'UTC');let parts:Record<string,string>;
  try{parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(date).map(p=>[p.type,p.value]));}catch{throw new QueryError('Invalid timezone');}
  const fmt=text(arg.format??(['UTC','Etc/UTC'].includes(zone)?'%Y-%m-%dT%H:%M:%S.%LZ':'%Y-%m-%dT%H:%M:%S.%L'));
  requireValue(fmt.length<=256&&!/%(?![YmdHMSL%])/.test(fmt.replace(/%%/g,'')),'Unsupported date format');
  const values:Record<string,string>={Y:parts.year.padStart(4,'0'),m:parts.month,d:parts.day,H:parts.hour,M:parts.minute,S:parts.second,L:String(date.getUTCMilliseconds()).padStart(3,'0'),'%':'%'};
  return fmt.replace(/%([YmdHMSL%])/g,(_,k:string)=>values[k]);
}
function expression(row: Document,value: Value): Value {
  if(typeof value==='string'&&value.startsWith('$'))return get(row,value.slice(1))??null;
  if(Array.isArray(value))return value.map(v=>expression(row,v));
  if(!object(value))return value;
  if(!Object.keys(value).some(k=>k.startsWith('$')))return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,expression(row,v)]));
  const [op,raw]=Object.entries(value)[0];if(op==='$literal')return clone(raw);
  if(op==='$cond'){const args=arr(raw);return expression(row,args[truth(expression(row,args[0]))?1:2]);}
  if(op==='$ifNull'){const args=arr(raw);return expression(row,args[0])??expression(row,args[1]);}
  if(op==='$and')return arr(raw).every(v=>truth(expression(row,v)));
  if(op==='$or')return arr(raw).some(v=>truth(expression(row,v)));
  const arg=expression(row,raw);
  if(op==='$eq'||op==='$ne'){const values=arr(arg);return equal(values[0],values[1])===(op==='$eq');}
  if(['$gt','$gte','$lt','$lte'].includes(op)){const values=arr(arg),c=compare(values[0],values[1]);return ({'$gt':c>0,'$gte':c>=0,'$lt':c<0,'$lte':c<=0} as Record<string,boolean>)[op];}
  if(op==='$not')return !truth(arr(arg)[0]);
  if(op==='$in'){const values=arr(arg);return arr(values[1]).some(v=>equal(v,values[0]));}
  if(op==='$size')return arr(arg).length;
  if(op==='$arrayElemAt'){const values=arr(arg),items=arr(values[0]);requireValue(numeric(values[1])&&Number.isInteger(values[1]),'Invalid array index');return items.at(values[1])??null;}
  if(op==='$concatArrays')return arr(arg).flatMap(arr);
  if(op==='$dateToString')return dateString(doc(arg));
  if(op==='$toLower'||op==='$toUpper')return op==='$toLower'?text(arg).toLowerCase():text(arg).toUpperCase();
  const values=arr(arg);
  if(op==='$concat'){requireValue(values.every(v=>typeof v==='string'),'$concat requires strings');return values.join('');}
  requireValue(values.every(numeric),op+' requires finite numbers');
  let result:number;
  if(op==='$add')result=values.reduce((a,b)=>a+b,0);
  else if(op==='$multiply')result=values.reduce((a,b)=>a*b,1);
  else if(op==='$subtract')result=values[0]-values[1];
  else{requireValue(values[1]!==0,'Division by zero');result=values[0]/values[1];}
  requireValue(numeric(result),'Expression overflow');return result;
}
export function validatePipeline(pipeline: Value): asserts pipeline is Document[] {
  validateValue(pipeline);requireValue(Array.isArray(pipeline)&&pipeline.length<=MAX_STAGES,'Pipeline must contain at most 32 stages');
  for(const stage of pipeline){
    requireValue(object(stage)&&Object.keys(stage).length===1,'Each stage needs exactly one operator');
    const [op,value]=Object.entries(stage)[0];
    if(op==='$match')validateFilter(value);
    else if(op==='$skip'||op==='$limit')requireValue(numeric(value)&&Number.isInteger(value)&&value>=0&&value<=MAX_ROWS&&(op!=='$limit'||value>0),op+' is out of range');
    else if(op==='$sort'){const fields=doc(value);requireValue(Object.keys(fields).length>0,'$sort needs fields');for(const[k,v]of Object.entries(fields)){field(k);requireValue(v===1||v===-1,'Sort direction must be 1 or -1');}}
    else if(['$project','$set','$addFields'].includes(op)){
      const fields=doc(value);requireValue(Object.keys(fields).length>0,op+' needs fields');for(const[k,v]of Object.entries(fields)){field(k);validateExpression(v);}
      if(op==='$project'){const values=Object.entries(fields).filter(([k])=>!['id','_id'].includes(k)).map(([,v])=>v);requireValue(!(values.some(v=>v!==0)&&values.some(v=>v===0)),'Cannot mix inclusion and exclusion projection');}
    }else if(op==='$unset'){const fields=typeof value==='string'?[value]:arr(value);requireValue(fields.length>0,'$unset needs fields');fields.forEach(field);}
    else if(op==='$count')field(value);
    else if(op==='$group'){
      const fields=doc(value);requireValue(Object.hasOwn(fields,'_id'),'$group needs _id');validateExpression(fields._id);
      for(const[k,v]of Object.entries(fields)){if(k==='_id')continue;field(k);const spec=doc(v);requireValue(Object.keys(spec).length===1&&accumulators.has(Object.keys(spec)[0]),'Unsupported accumulator');validateExpression(Object.values(spec)[0]);}
    }else if(op==='$unwind'){
      const config=typeof value==='string'?{path:value}:doc(value);
      requireValue(Object.keys(config).every(k=>['path','preserveNullAndEmptyArrays','includeArrayIndex'].includes(k)),'Invalid $unwind options');
      const path=text(config.path);requireValue(path.startsWith('$'),'$unwind path must start with $');field(path.slice(1));
      if(Object.hasOwn(config,'preserveNullAndEmptyArrays'))requireValue(typeof config.preserveNullAndEmptyArrays==='boolean','Invalid preserveNullAndEmptyArrays');
      if(Object.hasOwn(config,'includeArrayIndex'))field(config.includeArrayIndex);
    }else throw new QueryError('Unsupported pipeline stage: '+op,'UNSUPPORTED_QUERY');
  }
}
function byteBudget(rows:Document[],stage:Document,maxBytes:number){
  const [op,value]=Object.entries(stage)[0];let estimate=0;
  const expanding=['$set','$addFields','$project','$group'].includes(op);
  const copies=expanding?Math.max(1,Object.keys(doc(value)).length):1;
  for(const row of rows){
    let multiplier=copies;
    if(op==='$unwind'){const path=typeof value==='string'?value:text(doc(value).path);const items=get(row,path.slice(1));multiplier=Array.isArray(items)?Math.max(1,items.length):1;}
    estimate+=new TextEncoder().encode(JSON.stringify(row)).length*multiplier;
    if(estimate>maxBytes)throw new QueryError('Query expansion byte budget exceeded','QUERY_LIMIT',413);
  }
  if(expanding)estimate+=new TextEncoder().encode(JSON.stringify(value)).length*rows.length;
  if(estimate>maxBytes)throw new QueryError('Query expansion byte budget exceeded','QUERY_LIMIT',413);
}
export function aggregate(input: Document[], pipeline: Value, maxRows=MAX_ROWS,maxBytes=8388608): Document[] {
  validatePipeline(pipeline);let rows=clone(input);
  const bound=()=>{if(rows.length>maxRows)throw new QueryError('Query row limit exceeded','QUERY_LIMIT',413);};bound();
  for(const stage of pipeline){
    byteBudget(rows,stage,maxBytes);
    const[op,value]=Object.entries(stage)[0];
    if(op==='$match')rows=rows.filter(r=>matches(r,doc(value)));
    else if(op==='$skip')rows=rows.slice(value as number);
    else if(op==='$limit')rows=rows.slice(0,value as number);
    else if(op==='$sort')rows.sort((a,b)=>{for(const[k,direction]of Object.entries(doc(value))){const c=compare(get(a,k),get(b,k))*(direction as number);if(c)return c;}return 0;});
    else if(op==='$count')rows=rows.length?[{[text(value)]:rows.length}]:[];
    else if(op==='$set'||op==='$addFields')for(const row of rows){const computed=Object.fromEntries(Object.entries(doc(value)).map(([k,v])=>[k,expression(row,v)]));for(const[k,v]of Object.entries(computed))put(row,k,v);}
    else if(op==='$unset')for(const row of rows)for(const k of typeof value==='string'?[value]:arr(value))removeProjection(row,text(k).split('.'));
    else if(op==='$project'){
      const fields=doc(value),inclusion=Object.values(fields).some(v=>v!==0);
      rows=rows.map(row=>{const output:Document=inclusion?{}:clone(row);if(inclusion)for(const k of ['id','_id'])if(fields[k]!==0&&Object.hasOwn(row,k))output[k]=row[k];for(const[k,v]of Object.entries(fields)){if(v===0)removeProjection(output,k.split('.'));else if(v===1)mergeProjection(output,selection(row,k.split('.')));else put(output,k,expression(row,v));}return output;});
    }else if(op==='$unwind'){
      const config=typeof value==='string'?{path:value}:doc(value),path=text(config.path).slice(1),result:Document[]=[];
      for(const row of rows){const values=get(row,path);
        if(values==null||Array.isArray(values)&&!values.length){if(config.preserveNullAndEmptyArrays){const output=clone(row);if(Array.isArray(values))put(output,path,undefined);if(config.includeArrayIndex)put(output,text(config.includeArrayIndex),null);result.push(output);}}
        else{const array=Array.isArray(values);(array?values:[values]).forEach((v,i)=>{const output=clone(row);put(output,path,v);if(config.includeArrayIndex)put(output,text(config.includeArrayIndex),array?i:null);result.push(output);if(result.length>maxRows)throw new QueryError('Query expansion exceeds limit','QUERY_LIMIT',413);});}
      }rows=result;
    }else if(op==='$group'){
      const fields=doc(value),groups=new Map<string,{key:Value;members:Document[]}>();
      for(const row of rows){const key=expression(row,fields._id),s=canonical(key);if(!groups.has(s))groups.set(s,{key,members:[]});groups.get(s)!.members.push(row);}
      rows=[...groups.values()].map(({key,members})=>{const output:Document={_id:key};for(const[k,spec]of Object.entries(fields)){if(k==='_id')continue;const[acc,expr]=Object.entries(doc(spec))[0],values=members.map(r=>expression(r,expr)),nums=values.filter(numeric);
        if(acc==='$sum')output[k]=nums.reduce((a,b)=>a+b,0);
        else if(acc==='$avg')output[k]=nums.length?nums.reduce((a,b)=>a+b,0)/nums.length:null;
        else if(acc==='$min'||acc==='$max')output[k]=values.reduce((a,b)=>(acc==='$min'?compare(a,b)<=0:compare(a,b)>=0)?a:b);
        else if(acc==='$first')output[k]=values[0];else if(acc==='$last')output[k]=values.at(-1)!;else if(acc==='$push')output[k]=values;
        else output[k]=values.filter((v,i)=>values.findIndex(old=>equal(v,old))===i);
      }return output;});
    }bound();
  }
  validateValue(rows);return rows;
}
export function find(rows: Document[], filter: Document={}, options: Document={}): Document[] {
  requireValue(Object.keys(options).every(k=>['sort','skip','limit','projection'].includes(k)),'Unsupported query options');
  const pipeline:Document[]=[{$match:filter}];for(const k of ['sort','skip','limit','projection'])if(Object.hasOwn(options,k))pipeline.push({[k==='projection'?'$project':'$'+k]:options[k]});return aggregate(rows,pipeline);
}

/** Ordered update operators, applied to a copy. This is not an atomic DB update. */
export function updated(current:Document,update:Document):Document {
  validateValue(update);requireValue(object(update)&&Object.keys(update).length>0,'Update requires operators');
  const result=clone(current);const paths:string[]=[];
  for(const [op,changes] of Object.entries(update)){
    requireValue(['$set','$unset','$inc','$push','$addToSet','$pull'].includes(op),'Unsupported update operator: '+op);
    for(const [path,value] of Object.entries(doc(changes))){
      field(path);requireValue(!['id','_id'].includes(path.split('.')[0]),'Record identity cannot be updated');
      requireValue(!paths.some(p=>p===path||p.startsWith(path+'.')||path.startsWith(p+'.')),'Conflicting update paths');paths.push(path);
      const previous=get(result,path);
      if(op==='$set')put(result,path,value);
      else if(op==='$unset')put(result,path,undefined);
      else if(op==='$inc'){requireValue(numeric(value)&&(previous===undefined||numeric(previous)),'$inc requires numbers');const next=(previous===undefined?0:previous as number)+value;requireValue(numeric(next),'Update overflow');put(result,path,next);}
      else{let items=previous===undefined?[]:[...arr(previous)];
        if(op==='$push'||op==='$addToSet'){
          let additions=[value];if(object(value)&&Object.keys(value).some(k=>k.startsWith('$'))){requireValue(Object.keys(value).length===1&&Array.isArray(value.$each),'Only the $each array modifier is supported');additions=arr(value.$each);}
          for(const addition of additions)if(op==='$push'||!items.some(v=>equal(v,addition)))items.push(addition);
        }else{if(object(value))validateFilter(scalarCondition(value)?{value}:value);items=items.filter(v=>!elementMatches(v,value));}
        put(result,path,items);
      }
    }
  }
  return result;
}
export function distinct(rows:Document[],path:string):Value[]{
  field(path);const result:Value[]=[];
  for(const row of rows){const value=get(row,path);if(value===undefined)continue;for(const item of Array.isArray(value)?value:[value])if(!result.some(v=>equal(v,item)))result.push(clone(item));}
  return result;
}
export {validateValue};
