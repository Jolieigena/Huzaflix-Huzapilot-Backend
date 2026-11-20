"""Model facade used by every generated service. Override hooks in modules/.

Bulk operations are ordered and individually durable, not multi-record atomic.
Use the explicit native driver for database-specific transaction semantics.
"""
import copy
import json
import os
from jsonschema import Draft7Validator, FormatChecker
from .planner import plan, candidate_filter, candidate_ids
from .query import aggregate, find, validate_pipeline, validate_value, QueryError, canonical, get, put, MISSING, equal, field, updated

class Model:
    def __init__(self, store, name, schema):
        self.store, self.name, self.schema = store, name, schema
        Draft7Validator.check_schema(schema)
        self.validator = Draft7Validator(schema, format_checker=FormatChecker())
        self.max_rows = min(10000, max(1, int(os.getenv('QUERY_MAX_ROWS', '1000'))))
        self.max_bytes = min(67108864, max(1024, int(os.getenv('QUERY_MAX_BYTES', '8388608'))))

    def before_create(self, data): return data
    def after_create(self, data): pass
    def before_update(self, current, data): return data
    def after_update(self, data): pass
    def before_delete(self, current): pass
    def after_delete(self, previous): pass

    def _validate(self, data):
        if not isinstance(data, dict): raise QueryError('Record must be an object', 'VALIDATION_ERROR', 422)
        data = copy.deepcopy(data)
        data.pop('id', None); data.pop('_id', None)
        validate_value(data)
        errors = sorted(self.validator.iter_errors(data), key=lambda e: str(list(e.absolute_path)))
        if errors:
            error = errors[0]
            path = '.'.join(str(p) for p in error.absolute_path)
            raise QueryError((path + ': ' if path else '') + error.message, 'VALIDATION_ERROR', 422)
        return data

    def _snapshot(self): return self.store.scan(self.name, self.max_rows, self.max_bytes)

    def find(self, query=None, options=None):
        # Validate against an empty snapshot first: invalid queries never cause scans.
        find([], query, options)
        rows = self.store.query(self.name, self.schema, query, options, self.max_rows, self.max_bytes)
        if rows is not None: return rows
        candidate = candidate_filter(self.store.kind,self.schema,query or {}) if self.store.kind!='cassandra' else {}
        rows = self.store.query(self.name,self.schema,candidate,{},self.max_rows,self.max_bytes) if candidate else None
        return find(rows if rows is not None else self._snapshot(),query,options)

    def explain(self, query=None, options=None):
        """Planner report, not a database EXPLAIN or a guarantee of index use."""
        find([],query,options)
        native = candidate_ids(query or {}) is not None if self.store.kind=='cassandra' else plan('postgres' if self.store.kind=='mongodb' else self.store.kind,self.schema,query,options) is not None
        candidate = {} if native or self.store.kind=='cassandra' else candidate_filter(self.store.kind,self.schema,query or {})
        return {'database':self.store.kind,'execution':'primary-key' if native and self.store.kind=='cassandra' else 'native' if native else 'native-prefilter' if candidate else 'bounded-scan','maxRows':self.max_rows,'maxBytes':self.max_bytes,'atomicWrites':'single-record','bulkAtomic':False,'upsert':'id','crossModelHttpLookup':False}

    def find_page(self, query=None, size=50, after=None, total=False):
        """Keyset pagination by immutable id; totals are a separate read."""
        if isinstance(size,bool) or not isinstance(size,int) or not 1<=size<self.max_rows: raise QueryError('Page size must be smaller than QUERY_MAX_ROWS')
        if after is not None and (not isinstance(after,str) or not after or len(after)>36): raise QueryError('Invalid page cursor')
        predicate = {'$and':[query or {}, {'id':{'$gt':after}}]} if after is not None else query or {}
        rows = self.find(predicate, {'sort':{'id':1},'limit':size+1})
        items = rows[:size]; more = len(rows)>size
        result = {'items':items, 'hasMore':more, 'nextAfter':items[-1]['id'] if more else None}
        if total: result['total']=self.count_documents(query)
        return result

    def find_one(self, query=None, options=None):
        rows = self.find(query, {**(options or {}), 'limit': 1})
        return rows[0] if rows else None

    def find_by_id(self, id):
        rows = self.store.list(self.name, id)
        return rows[0] if rows else None

    def count_documents(self, query=None):
        find([], query, {})
        count = self.store.query(self.name, self.schema, query, {}, self.max_rows, self.max_bytes, count=True)
        return count if count is not None else len(self.find(query))

    def distinct(self, path, query=None):
        field(path); result = []
        for row in self.find(query):
            value = get(row, path)
            if value is MISSING: continue
            for item in value if isinstance(value, list) else [value]:
                if not any(equal(item, previous) for previous in result): result.append(item)
        return result

    def aggregate(self, pipeline):
        validate_pipeline(pipeline)
        remaining = list(pipeline)
        predicate = remaining.pop(0)['$match'] if remaining and '$match' in remaining[0] else {}
        if len(remaining) == 1 and '$count' in remaining[0]:
            count = self.count_documents(predicate)
            return [{remaining[0]['$count']:count}] if count else []
        options = {}
        for op, name in (('$sort','sort'),('$skip','skip'),('$limit','limit')):
            if remaining and op in remaining[0]: options[name] = remaining.pop(0)[op]
        rows = self.store.group(self.name,self.schema,predicate,remaining[0]['$group'],self.max_rows,self.max_bytes) if not options and remaining and '$group' in remaining[0] else None
        if rows is not None: remaining.pop(0)
        else: rows = self.find(predicate, options)
        result = aggregate(rows, remaining, self.max_rows, self.max_bytes)
        if len(json.dumps(result).encode()) > self.max_bytes: raise QueryError('Query result byte limit exceeded', 'QUERY_LIMIT', 413)
        return result

    def create(self, data):
        data = self._validate(self.before_create(copy.deepcopy(data)))
        result = self.store.save(self.name, data)
        self.after_create(copy.deepcopy(result))
        return result

    def upsert_by_id(self, id, update, on_insert):
        """Apply operators once, inserting validated defaults if the ID is absent.

        Before hooks may retry; after hooks run after a successful commit. This
        does not promise exactly-once processing across caller/network retries.
        """
        if not isinstance(id,str) or not id or len(id)>36: raise QueryError('Invalid upsert id')
        updated({}, update)
        initial = self._validate(on_insert)
        for _ in range(16):
            result = self._mutate(id, lambda current: updated(current,update))
            if result is not None: return {'record':result[0], 'inserted':False}
            data = self._validate(self.before_create(updated(copy.deepcopy(initial),update)))
            if self.store.insert_if_absent(self.name,id,data):
                row={**data,'id':id}; self.after_create(copy.deepcopy(row))
                return {'record':row,'inserted':True}
        raise QueryError('Concurrent upsert; retry the operation','WRITE_CONFLICT',409)

    def insert_many(self, records):
        if not isinstance(records, list) or len(records) > self.max_rows: raise QueryError('Invalid bulk size')
        # Validate all inputs before the first write; hooks still validate each final record.
        for record in records: self._validate(record)
        return [self.create(record) for record in records]

    def _mutate(self, id, transform, query=None):
        # Hooks before a write may run again after a conflict; keep them pure.
        import time, random
        for attempt in range(16):
            current, token = self.store.snapshot(self.name, id)
            if current is None or query is not None and not find([current], query): return None
            data = self._validate(self.before_update(copy.deepcopy(current), transform(copy.deepcopy(current))))
            previous = {k:v for k,v in current.items() if k not in ('id','_id')}
            if equal(previous, data): return current, False
            if self.store.compare_write(self.name, id, token, data):
                result = {**data, 'id':id}; self.after_update(copy.deepcopy(result)); return result, True
            time.sleep(random.uniform(0, min(.05, .001 * 2 ** attempt)))
        raise QueryError('Concurrent update; retry the operation', 'WRITE_CONFLICT', 409)

    def update_by_id(self, id, changes):
        if not isinstance(changes, dict): raise QueryError('Update must be an object')
        result = self._mutate(id, lambda current: {**current, **copy.deepcopy(changes)})
        if result is None: raise QueryError('Not found', 'NOT_FOUND', 404)
        return result[0]

    def _delete(self, id, query=None):
        current, token = self.store.snapshot(self.name, id)
        if current is None or query is not None and not find([current], query): return False
        self.before_delete(copy.deepcopy(current))
        if not self.store.compare_write(self.name, id, token): raise QueryError('Concurrent update; retry the operation', 'WRITE_CONFLICT', 409)
        self.after_delete(copy.deepcopy(current)); return True

    def delete_by_id(self, id):
        if not self._delete(id): raise QueryError('Not found', 'NOT_FOUND', 404)
        return {'deleted': True}

    def _updated(self, current, update): return self._validate(updated(current, update))

    def _update(self, query, update, many):
        updated({}, update)
        rows = self.find(query, {} if many else {'limit': 1})
        for row in rows: self._updated(row, update)
        matched, modified = 0, 0
        for row in rows:
            result = self._mutate(row['id'], lambda current: self._updated(current, update), query or {})
            if result is not None: matched += 1; modified += int(result[1])
        return {'acknowledged': True, 'matchedCount': matched, 'modifiedCount': modified}

    def update_one(self, query, update): return self._update(query, update, False)
    def update_many(self, query, update): return self._update(query, update, True)

    def delete_many(self, query):
        rows = self.find(query)
        deleted = sum(int(self._delete(row['id'], query or {})) for row in rows)
        return {'acknowledged': True, 'deletedCount': deleted}

    def delete_one(self, query):
        row = self.find_one(query)
        return {'acknowledged': True, 'deletedCount': int(bool(row) and self._delete(row['id'], query or {}))}

    def populate(self, rows, relation, target, authorize):
        """Server-only join; the caller must supply the target read policy.

        authorize(target_name) returns a validated read filter or None to deny.
        The target facade is explicit so custom service find behavior is retained.
        """
        spec=self.schema.get('x-huza-relations',{}).get(relation)
        if spec is None or target.store is not self.store or target.name!=spec['model']: raise QueryError('Unknown relationship or target model')
        if not callable(authorize): raise QueryError('Relationship read policy required','FORBIDDEN',403)
        policy=authorize(target.name)
        if policy is None: raise QueryError('Related model access denied','FORBIDDEN',403)
        find([],policy,{})
        if not isinstance(rows,list) or len(rows)>self.max_rows or len(json.dumps(rows).encode())>self.max_bytes: raise QueryError('Relationship input limit exceeded','QUERY_LIMIT',413)
        keys={}
        for row in rows:
            value=get(row,spec['localField'])
            if value is not MISSING and value is not None: keys[canonical(value)]=value
        related=target.find({'$and':[policy,{spec['foreignField']:{'$in':list(keys.values())}}]}) if keys else []
        groups={}
        for row in related:
            value=get(row,spec['foreignField'])
            if value is not MISSING and value is not None: groups.setdefault(canonical(value),[]).append(row)
        result=[];size=0
        for source in rows:
            value=get(source,spec['localField']); matches=groups.get(canonical(value),[]) if value is not MISSING and value is not None else []
            if not spec['many'] and len(matches)>1: raise QueryError('Singular relationship is ambiguous','RELATION_CONFLICT',409)
            row={**source,relation:matches if spec['many'] else matches[0] if matches else None}
            size+=len(json.dumps(row).encode())
            if size>self.max_bytes: raise QueryError('Relationship result limit exceeded','QUERY_LIMIT',413)
            result.append(copy.deepcopy(row))
        return result
