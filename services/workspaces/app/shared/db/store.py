from app.shared.monitoring import observe
from ..cache import ResponseCache
import json
import os
import uuid
from sqlalchemy import create_engine, MetaData, Table, Column, String, Text, select
from sqlalchemy.engine import URL
from sqlalchemy import text, event
from .planner import plan, mongo_plan, group_plan, candidate_ids

def write_errors(operation):
    """Keep driver details private and give uniqueness conflicts a stable API code."""
    from functools import wraps
    @wraps(operation)
    def invoke(*args, **kwargs):
        try: return operation(*args, **kwargs)
        except Exception as error:
            from .query import QueryError
            from pymongo.errors import DuplicateKeyError
            original=getattr(error,'orig',error)
            unique=isinstance(error,DuplicateKeyError) or getattr(original,'sqlstate',None)=='23505' or getattr(original,'sqlite_errorname',None) in ('SQLITE_CONSTRAINT_UNIQUE','SQLITE_CONSTRAINT_PRIMARYKEY') or getattr(original,'args',())[0:1]==(1062,)
            if unique: raise QueryError('Unique constraint violation','UNIQUE_CONFLICT',409) from error
            raise
    return invoke

class Store:
    def __init__(self, modules):
        self.cache = ResponseCache()
        self.kind = os.getenv('DB_TYPE', 'postgres')
        self.tables = {}
        pool_size=min(32,max(1,int(os.getenv('DB_POOL_SIZE','4'))))
        host, user, password, name = [os.getenv('DB_' + key) for key in ('HOST','USER','PASSWORD','NAME')]
        if self.kind == 'mongodb':
            from pymongo import MongoClient
            self.client = MongoClient(host, username=user, password=password, maxPoolSize=pool_size, waitQueueTimeoutMS=10000, serverSelectionTimeoutMS=10000)
            self.client.admin.command('ping')
            self.db = self.client[name]
        elif self.kind == 'cassandra':
            from cassandra.cluster import Cluster
            from cassandra.auth import PlainTextAuthProvider
            self.cluster = Cluster([host], auth_provider=PlainTextAuthProvider(user,password))
            self.db = self.cluster.connect()
            self.db.execute(f"CREATE KEYSPACE IF NOT EXISTS {name} WITH replication = {{'class':'SimpleStrategy','replication_factor':1}}")
            self.db.set_keyspace(name)
            for module in modules:
                self.db.execute(f'CREATE TABLE IF NOT EXISTS {self.table(module)} (id text PRIMARY KEY, data text)')
        else:
            url = URL.create('sqlite', database='/data/app.sqlite') if self.kind == 'sqlite' else URL.create({'postgres':'postgresql+psycopg','mysql':'mysql+pymysql'}[self.kind], username=user,password=password,host=host,port=int(os.environ['DB_PORT']),database=name)
            self.engine = create_engine(url, pool_pre_ping=True, pool_size=1 if self.kind=='sqlite' else pool_size, max_overflow=0, pool_timeout=10, pool_recycle=1800)
            if self.kind=='sqlite':
                @event.listens_for(self.engine,'connect')
                def sqlite_settings(connection, _):
                    cursor=connection.cursor();cursor.execute('PRAGMA busy_timeout=10000');cursor.execute('PRAGMA journal_mode=WAL');cursor.close()
            meta = MetaData()
            for module in modules:
                self.tables[module] = Table(self.table(module),meta,Column('id',String(36,collation={'postgres':'C','mysql':'utf8mb4_0900_bin','sqlite':'BINARY'}[self.kind]),primary_key=True),Column('data',Text,nullable=False))
            from sqlalchemy.schema import CreateTable
            for module, table in self.tables.items():
                with self.engine.begin() as conn:
                    if self.kind=='postgres': conn.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'),{'key':'huzapilot:'+self.table(module)})
                    conn.execute(CreateTable(table,if_not_exists=True))
        self.ensure_indexes(modules)

    def ensure_indexes(self, modules):
        from pathlib import Path
        path=Path('db-indexes.json')
        if not path.exists(): return
        for index in json.loads(path.read_text()):
            if index['module'] not in modules: continue
            table=self.table(index['module'])
            if self.kind=='mongodb':
                options={'name':index['name'],'unique':index['unique'],'collation':{'locale':'simple'}}
                if index['unique']: options['partialFilterExpression']=index['partialFilter']
                self.db[table].create_index([(v['path'],v['direction']) for v in index['fields']],**options)
            elif self.kind!='cassandra':
                with self.engine.begin() as conn:
                    if self.kind=='postgres': conn.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'),{'key':'huzapilot:'+table})
                    if self.kind=='mysql' and conn.execute(text('SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name=:table AND index_name=:name'),{'table':table,'name':index['name']}).scalar(): continue
                    try: conn.execute(text(index['sql']))
                    except Exception as error:
                        # Another service replica can finish the same DDL first.
                        if self.kind!='mysql' or getattr(error,'orig',None) is None or error.orig.args[0]!=1061: raise

    def table(self, module):
        return 'hp_' + module.replace('-', '_')
    @observe("list")
    def list(self, module, id=None):
        table = self.table(module)
        if self.kind == 'mongodb':
            return [{**{k:v for k,v in row.items() if k!='_id'},'id':row['_id']} for row in self.db[table].find({'_id':id} if id else {})]
        if self.kind == 'cassandra':
            rows = self.db.execute(f'SELECT id,data FROM {table}' + (' WHERE id=%s' if id else ''), [id] if id else None)
            return [{**json.loads(r.data),'id':r.id} for r in rows]
        table = self.tables[module]
        with self.engine.connect() as conn:
            query = select(table)
            if id: query = query.where(table.c.id == id)
            return [{**json.loads(r.data),'id':r.id} for r in conn.execute(query)]
    @write_errors
    def _save_uncached(self, module, data, id=None):
        fresh = id is None
        id = id or str(uuid.uuid4())
        data = {k:v for k,v in data.items() if k not in ('id','_id')}
        table = self.table(module)
        if self.kind == 'mongodb': self.db[table].replace_one({'_id':id},data,upsert=True)
        elif self.kind == 'cassandra': self.db.execute(f'INSERT INTO {table} (id,data) VALUES (%s,%s)',[id,json.dumps(data)])
        else:
            table = self.tables[module]
            with self.engine.begin() as conn:
                if fresh: conn.execute(table.insert().values(id=id,data=json.dumps(data)))
                else: conn.execute(table.update().where(table.c.id==id).values(data=json.dumps(data)))
        return {**data,'id':id}
    def _remove_uncached(self,module,id):
        table=self.table(module)
        if self.kind=='mongodb': self.db[table].delete_one({'_id':id})
        elif self.kind=='cassandra': self.db.execute(f'DELETE FROM {table} WHERE id=%s',[id])
        else:
            table=self.tables[module]
            with self.engine.begin() as conn: conn.execute(table.delete().where(table.c.id==id))

    @observe("scan")
    def scan(self, module, max_rows, max_bytes):
        """Stream a bounded snapshot; exceeding a bound fails, never truncates."""
        from .query import QueryError
        from contextlib import ExitStack
        from cassandra.query import SimpleStatement
        rows, total = [], 0
        def append(record, size):
            nonlocal total
            total += size
            if len(rows) >= max_rows or total > max_bytes:
                raise QueryError('Query scan limit exceeded; narrow the data or use an explicit native query', 'QUERY_LIMIT', 413)
            rows.append(record)
        with ExitStack() as resources:
            if self.kind == 'mongodb':
                cursor = self.db[self.table(module)].find({}).limit(max_rows + 1).batch_size(64)
                resources.callback(cursor.close)
                for record in cursor:
                    record['id'] = record.pop('_id')
                    append(record, len(json.dumps(record).encode()))
            elif self.kind == 'cassandra':
                statement = SimpleStatement(f'SELECT id,data FROM {self.table(module)} LIMIT {int(max_rows) + 1}', fetch_size=64)
                for record in self.db.execute(statement):
                    append({**json.loads(record.data), 'id': record.id}, len(record.data.encode()))
            else:
                connection = resources.enter_context(self.engine.connect())
                result = resources.enter_context(connection.execution_options(stream_results=True).execute(select(self.tables[module]).limit(max_rows + 1)))
                for record in result:
                    append({**json.loads(record.data), 'id': record.id}, len(record.data.encode()))
        return rows

    @observe("query")
    def query(self, module, schema, query, options, max_rows, max_bytes, count=False):
        """Return None when the planner cannot preserve portable semantics."""
        from .query import QueryError
        query, options = query or {}, options or {}
        if self.kind == 'cassandra':
            identities = candidate_ids(query)
            if identities is None: return None
            if len(identities)>max_rows: raise QueryError('Query partition limit exceeded','QUERY_LIMIT',413)
            rows=[];size=0
            for identity in identities:
                for row in self.list(module,identity):
                    size+=len(json.dumps(row).encode())
                    if size>max_bytes: raise QueryError('Query result byte limit exceeded','QUERY_LIMIT',413)
                    rows.append(row)
            from .query import find
            return len(find(rows,query,{})) if count else find(rows,query,options)
        if self.kind == 'mongodb':
            compiled = mongo_plan(schema, query, options)
            if compiled is None: return None
            collection = self.db[self.table(module)]
            if count: return collection.count_documents(compiled)
            sort = [('_id' if k=='id' else k, int(v)) for k,v in options.get('sort',{}).items()]
            if 'id' not in options.get('sort',{}): sort.append(('_id',1))
            fetch = int(options['limit']) if 'limit' in options and options['limit']<=max_rows else max_rows+1
            rows, size = [], 0
            with collection.find(compiled).sort(sort).skip(int(options.get('skip',0))).limit(fetch).batch_size(64) as cursor:
                for record in cursor:
                    record['id'] = record.pop('_id'); size += len(json.dumps(record).encode())
                    if len(rows)>=max_rows or size>max_bytes: raise QueryError('Query result limit exceeded','QUERY_LIMIT',413)
                    rows.append(record)
            from .query import find
            return find(rows,{}, {'projection':options['projection']} if 'projection' in options else {})
        compiled = plan(self.kind, schema, query, options)
        if compiled is None: return None
        limit = min(int(options.get('limit', max_rows)), max_rows)
        offset = int(options.get('skip', 0))
        # One extra row detects an unpaged result exceeding the configured bound.
        fetch = limit if 'limit' in options and options['limit'] <= max_rows else max_rows + 1
        params = {f'p{i}': value for i, value in enumerate(compiled['params'])}
        parts = iter(range(len(params)))
        where = __import__('re').sub(r'\?', lambda _: ':p'+str(next(parts)), compiled['where'])
        statement = f"SELECT {'COUNT(*)' if count else 'id,data'} FROM {self.table(module)} WHERE {where}"
        if not count:
            statement += ' ORDER BY ' + compiled['order'] + ' LIMIT :take OFFSET :skip'
            params.update(take=fetch, skip=offset)
        with self.engine.connect() as connection:
            result = connection.execution_options(stream_results=True).execute(text(statement), params)
            if count: return int(result.scalar_one())
            rows, size = [], 0
            for record in result:
                size += len(record.data.encode())
                if len(rows) >= max_rows or size > max_bytes: raise QueryError('Query result limit exceeded', 'QUERY_LIMIT', 413)
                rows.append({**json.loads(record.data), 'id': record.id})
        from .query import find
        return find(rows, {}, {'projection': options['projection']} if 'projection' in options else {})

    @observe("group")
    def group(self, module, schema, predicate, group, max_rows, max_bytes):
        from .query import QueryError
        from contextlib import ExitStack
        compiled=group_plan(self.kind,schema,predicate,group)
        if compiled is None: return None
        rows=[];size=0
        def append(record):
            nonlocal size
            if not record['hp_count']: return
            key=compiled['key'] if compiled['constant'] else record['_id' if self.kind=='mongodb' else 'hp_key']
            if not compiled['constant'] and key is not None:
                if compiled['keyType'] in ('integer','number'): key=float(key)
                elif compiled['keyType']=='boolean': key=bool(key)
                elif isinstance(key,bytes): key=key.decode()
            row={'_id':key}
            for column in compiled['columns']:
                value=record[column['alias']]
                if column['nullAlias'] and record[column['nullAlias']]: value=None
                row[column['name']]=None if value is None else float(value)
            size+=len(json.dumps(row).encode())
            if len(rows)>=max_rows or size>max_bytes: raise QueryError('Query result limit exceeded','QUERY_LIMIT',413)
            rows.append(row)
        with ExitStack() as resources:
            if self.kind=='mongodb':
                cursor=self.db[self.table(module)].aggregate(compiled['mongo']+[{'$limit':max_rows+1}],batchSize=64,allowDiskUse=False,maxTimeMS=15000)
                resources.callback(cursor.close)
                for record in cursor: append(record)
            else:
                params={f'p{i}':value for i,value in enumerate(compiled['params'])};parts=iter(range(len(params)))
                statement='SELECT '+compiled['select']+' FROM '+self.table(module)+' WHERE '+compiled['where']
                if compiled['groupBy']: statement+=' GROUP BY '+compiled['groupBy']
                statement+=' LIMIT '+str(max_rows+1)
                statement=__import__('re').sub(r'\?',lambda _:':p'+str(next(parts)),statement)
                connection=resources.enter_context(self.engine.connect())
                result=resources.enter_context(connection.execution_options(stream_results=True).execute(text(statement),params))
                for record in result.mappings(): append(record)
        return rows

    @observe("snapshot")
    def snapshot(self, module, id):
        """Read the exact persisted value used as an optimistic concurrency token."""
        table = self.table(module)
        if self.kind == 'mongodb':
            token = self.db[table].find_one({'_id':id})
            return ({**{k:v for k,v in token.items() if k!='_id'},'id':id},token) if token is not None else (None,None)
        if self.kind == 'cassandra':
            row = self.db.execute(f'SELECT data FROM {table} WHERE id=%s',[id]).one()
        else:
            with self.engine.connect() as conn: row = conn.execute(select(self.tables[module].c.data).where(self.tables[module].c.id==id)).first()
        return ({**json.loads(row.data),'id':id},row.data) if row is not None else (None,None)

    @write_errors
    def _compare_write_uncached(self, module, id, token, data=None):
        """Atomic single-record replacement/deletion, never an implicit upsert."""
        table = self.table(module)
        body = {k:v for k,v in data.items() if k not in ('id','_id')} if data is not None else None
        if self.kind == 'mongodb':
            predicate = {'_id':id,'$expr':{'$eq':['$$ROOT',{'$literal':token}]}}
            if body is None: return self.db[table].delete_one(predicate).deleted_count == 1
            return self.db[table].replace_one(predicate,body).matched_count == 1
        if self.kind == 'cassandra':
            statement = f'DELETE FROM {table} WHERE id=%s IF data=%s' if body is None else f'UPDATE {table} SET data=%s WHERE id=%s IF data=%s'
            args = [id,token] if body is None else [json.dumps(body),id,token]
            return self.db.execute(statement,args).one().applied
        comparison = 'CAST(data AS BINARY)=CAST(:old AS BINARY)' if self.kind=='mysql' else 'data=:old'
        statement = f'DELETE FROM {table} WHERE id=:id AND {comparison}' if body is None else f'UPDATE {table} SET data=:new WHERE id=:id AND {comparison}'
        args = {'id':id,'old':token}
        if body is not None: args['new']=json.dumps(body)
        with self.engine.begin() as conn: return conn.execute(text(statement),args).rowcount == 1

    def _insert_if_absent_uncached(self, module, id, data):
        """Insert-only; a collision on another unique key remains an error."""
        from .query import QueryError
        body = {k:v for k,v in data.items() if k not in ('id','_id')}
        try:
            if self.kind == 'mongodb': self.db[self.table(module)].insert_one({**body,'_id':id})
            elif self.kind == 'cassandra':
                return self.db.execute(f'INSERT INTO {self.table(module)} (id,data) VALUES (%s,%s) IF NOT EXISTS',[id,json.dumps(body)]).one().applied
            else:
                with self.engine.begin() as conn: conn.execute(self.tables[module].insert().values(id=id,data=json.dumps(body)))
            return True
        except Exception as error:
            @write_errors
            def rethrow(): raise error
            try: rethrow()
            except QueryError as conflict:
                if conflict.code == 'UNIQUE_CONFLICT' and self.snapshot(module,id)[0] is not None: return False
                raise

    def native(self):
        """Explicit database-specific escape hatch; not part of portable semantics."""
        return self.engine if hasattr(self, 'engine') else self.db

    def close(self):
        self.cache.close()
        if hasattr(self, 'engine'): self.engine.dispose()
        if hasattr(self, 'client'): self.client.close()
        if hasattr(self, 'cluster'): self.cluster.shutdown()

    @observe("save")
    def save(self, module, data, id=None):
        result = self._save_uncached(module,data,id)
        self.cache.invalidate(module)
        return result

    @observe("remove")
    def remove(self, module, id):
        result = self._remove_uncached(module,id)
        self.cache.invalidate(module)
        return result

    @observe("compare_write")
    def compare_write(self, module, id, token, data=None):
        applied = self._compare_write_uncached(module,id,token,data)
        if applied: self.cache.invalidate(module)
        return applied

    @observe("insert_if_absent")
    def insert_if_absent(self, module, id, data):
        applied = self._insert_if_absent_uncached(module,id,data)
        if applied: self.cache.invalidate(module)
        return applied
