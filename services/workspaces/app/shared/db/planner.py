"""Conservative SQL planner. Unsupported semantics return None for bounded fallback.

Only schema-declared scalar paths are eligible. Values use bind parameters; field
identifiers are validated. Never push pagination below an unevaluated predicate.
"""
import re

class Unsupported(Exception): pass

def scalar_column(kind, schema, path):
    if path == 'id': return 'id', "'string'", 'string'
    node = schema
    parts = path.split('.')
    for part in parts:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', part): raise Unsupported()
        node = node.get('properties', {}).get(part, {})
        # Arrays and union schemas require the portable evaluator.
        if node.get('type') == 'array': raise Unsupported()
    typ = node.get('type')
    if isinstance(typ, list):
        types = [item for item in typ if item != 'null']; typ = types[0] if len(types)==1 else None
    if typ not in ('string', 'integer', 'number', 'boolean'): raise Unsupported()
    if kind == 'postgres':
        raw = "data::jsonb #> '{" + ','.join(parts) + "}'"
        tag = f'jsonb_typeof({raw})'
        scalar = "data::jsonb #>> '{" + ','.join(parts) + "}'"
        value = f'({scalar}) COLLATE "C"' if typ == 'string' else f'CAST({scalar} AS {"boolean" if typ == "boolean" else "numeric"})'
    else:
        path_sql = '$.' + '.'.join('"' + p + '"' for p in parts)
        raw = f"json_extract(data, '{path_sql}')"
        tag = f"json_type(data, '{path_sql}')" if kind == 'sqlite' else f'LOWER(JSON_TYPE({raw}))'
        value = raw if kind == 'sqlite' else f"NULLIF(JSON_UNQUOTE({raw}), 'null')" if typ != 'string' else f"CASE WHEN {tag}='null' THEN NULL ELSE JSON_UNQUOTE({raw}) END"
        if typ == 'string':
            length = node.get('maxLength')
            value = f'({value}) COLLATE BINARY' if kind == 'sqlite' else f'CAST(({value}) AS CHAR({length}) CHARACTER SET utf8mb4) COLLATE utf8mb4_0900_bin' if isinstance(length,int) and 1<=length<=191 else f'CAST(({value}) AS BINARY)'
        elif kind == 'mysql': value = f"CASE WHEN {tag} IS NULL OR {tag}='null' THEN NULL ELSE ({raw}=true) END" if typ == 'boolean' else f'CAST({value} AS DOUBLE)'
    return value, tag, typ

def plan(kind, schema, query, options):
    if kind not in ('postgres', 'mysql', 'sqlite'): return None
    params = []
    def bind(value):
        params.append(value)
        return '?'
    def column(path): return scalar_column(kind, schema, path)
    def join(parts, op): return '(' + f' {op} '.join(parts) + ')' if parts else ('1=1' if op=='AND' else '1=0')
    def clause(path, condition):
        value, tag, typ = column(path)
        ops = condition if isinstance(condition, dict) and any(k.startswith('$') for k in condition) else {'$eq': condition}
        out = []
        for op, arg in ops.items():
            if op == '$exists': out.append(f'{tag} IS {"NOT " if arg else ""}NULL'); continue
            if op == '$not': out.append('NOT '+clause(path,arg)); continue
            if op in ('$in', '$nin'):
                sql = join([clause(path, {'$eq': v}) for v in arg], 'OR')
                out.append(('NOT ' if op=='$nin' else '') + sql); continue
            if op not in ('$eq', '$ne', '$gt', '$gte', '$lt', '$lte'): raise Unsupported()
            if arg is None:
                sql = f"({tag} IS NULL OR {tag}='null')" if op in ('$eq','$ne') else '1=0'
            else:
                compatible = isinstance(arg,str) if typ=='string' else isinstance(arg,bool) if typ=='boolean' else isinstance(arg,(int,float)) and not isinstance(arg,bool)
                if not compatible or typ=='boolean' and op not in ('$eq','$ne'): sql='1=0'
                else:
                    operator={'$eq':'=','$ne':'=','$gt':'>','$gte':'>=','$lt':'<','$lte':'<='}[op]
                    sql=f'({value} IS NOT NULL AND {value} {operator} {bind(arg)})'
            out.append('NOT ('+sql+')' if op=='$ne' else '('+sql+')')
        return join(out,'AND')
    def walk(query):
        out=[]
        for key,value in query.items():
            if key in ('$and','$or','$nor'):
                sql=join([walk(v) for v in value], 'AND' if key=='$and' else 'OR')
                out.append('NOT '+sql if key=='$nor' else sql)
            elif key.startswith('$'): raise Unsupported()
            else: out.append(clause(key,value))
        return join(out,'AND')
    try:
        where=walk(query or {})
        order=[]
        for path,direction in (options or {}).get('sort',{}).items():
            value,_,_=column(path)
            # Portable order puts null/missing first ascending, last descending.
            order.extend([f'({value} IS NOT NULL) {"ASC" if direction==1 else "DESC"}', f'{value} {"ASC" if direction==1 else "DESC"}'])
        if 'id' not in (options or {}).get('sort',{}): order.append('id ASC')
        return {'where':where,'params':params,'order':', '.join(order)}
    except Unsupported: return None


def mongo_plan(schema, query, options):
    if plan('postgres', schema, query, options) is None: return None
    def join(items, op): return {op:items} if items else ({} if op=='$and' else {'$expr':False})
    def clause(path, condition):
        path = '_id' if path=='id' else path
        ops = condition if isinstance(condition,dict) and any(k.startswith('$') for k in condition) else {'$eq':condition}
        out=[]
        for op,arg in ops.items():
            if op=='$not': value={'$nor':[clause(path,arg)]}
            elif op in ('$in','$nin'):
                value=join([clause(path,{'$eq':v}) for v in arg], '$or')
                if op=='$nin': value={'$nor':[value]}
            elif op=='$ne': value={'$nor':[{path:{'$eq':arg}}]}
            elif op in ('$gt','$gte','$lt','$lte') and (isinstance(arg,bool) or not isinstance(arg,(str,int,float))): value={'$expr':False}
            else: value={path:{op:arg}}
            out.append(value)
        return join(out,'$and')
    def walk(query):
        out=[]
        for key,value in query.items():
            if key in ('$and','$or','$nor'): out.append(join([walk(v) for v in value],key))
            else: out.append(clause(key,value))
        return join(out,'$and')
    return walk(query or {})


def group_plan(kind, schema, query, group):
    """Native scalar grouping with count/sum/average/min/max; other forms fall back."""
    if kind=='cassandra': return None
    compiled=plan('postgres' if kind=='mongodb' else kind,schema,query,{})
    if compiled is None: return None
    params=[];columns=[];select=[];mongo={'hp_count':{'$sum':1}}
    key=group['_id'];constant=True;key_type='literal';key_sql='NULL'
    try:
        if isinstance(key,str) and key.startswith('$'):
            constant=False;key_sql,_,key_type=scalar_column('postgres' if kind=='mongodb' else kind,schema,key[1:])
            mongo['_id']={'$ifNull':['$_id' if key=='$id' else key,None]}
        elif key is None or isinstance(key,(str,int,float,bool)): mongo['_id']={'$literal':key}
        else: return None
        select=[key_sql+' AS hp_key','COUNT(*) AS hp_count']
        for name,spec in group.items():
            if name=='_id': continue
            op,arg=next(iter(spec.items()))
            if op not in ('$sum','$avg','$min','$max'): return None
            alias='hp_a'+str(len(columns));numeric=False;term='CAST(NULL AS DECIMAL)';expression={'$literal':arg}
            if isinstance(arg,str) and arg.startswith('$'):
                value,_,typ=scalar_column('postgres' if kind=='mongodb' else kind,schema,arg[1:]);numeric=typ in ('integer','number')
                if numeric: term=value
                expression='$_id' if arg=='$id' else arg
            elif isinstance(arg,(float,int)) and not isinstance(arg,bool): numeric=True;term='CAST(? AS '+('DOUBLE' if kind=='mysql' else 'DOUBLE PRECISION')+')';params.append(arg)
            elif isinstance(arg,(dict,list)): return None
            if op in ('$min','$max') and not numeric: return None
            operation=op[1:].upper();sql=f'{operation}({term})'
            if op=='$sum': sql=f'COALESCE({sql},0)'
            # Portable min includes missing/null values; SQL/Mongo accumulators skip them.
            null_alias='hp_n'+str(len(columns)) if op=='$min' else None
            if null_alias:
                select.append(f'COUNT(*)-COUNT({term}) AS {null_alias}')
                if term.startswith('CAST(?'): params.append(arg)
                mongo[null_alias]={'$sum':{'$cond':[{'$eq':[{'$ifNull':[expression,None]},None]},1,0]}}
            select.append(sql+' AS '+alias);mongo[alias]={op:expression}
            columns.append({'name':name,'alias':alias,'nullAlias':null_alias})
        predicate=mongo_plan(schema,query,{}) if kind=='mongodb' else None
        return {'select':', '.join(select),'where':compiled['where'],'params':params+compiled['params'],'groupBy':None if constant else key_sql,'key':key,'constant':constant,'keyType':key_type,'columns':columns,'mongo':[{'$match':predicate},{'$group':mongo}]}
    except Unsupported: return None


def candidate_ids(predicate):
    """A positive ID constraint bounds even a complex residual predicate."""
    candidates=[]
    value=predicate.get('id')
    if isinstance(value,str): candidates.append([value])
    elif isinstance(value,dict):
        if isinstance(value.get('$eq'),str): candidates.append([value['$eq']])
        if isinstance(value.get('$in'),list) and all(isinstance(v,str) for v in value['$in']): candidates.append(list(dict.fromkeys(value['$in'])))
    for child in predicate.get('$and',[]):
        values=candidate_ids(child)
        if values is not None: candidates.append(values)
    if not candidates: return None
    return [v for v in candidates[0] if all(v in c for c in candidates[1:])]


def candidate_filter(kind,schema,predicate):
    clauses=[]
    for key,value in (predicate or {}).items():
        if key=='$and':
            for child in value:
                candidate=candidate_filter(kind,schema,child)
                if candidate: clauses.append(candidate)
        elif plan('postgres' if kind=='mongodb' else kind,schema,{key:value},{}) is not None: clauses.append({key:value})
    return {'$and':clauses} if clauses else {}
