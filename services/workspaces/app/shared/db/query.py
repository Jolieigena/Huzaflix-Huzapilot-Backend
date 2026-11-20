"""Versioned portable document queries. No eval, native query strings or silent stages.

The engine deliberately evaluates a bounded snapshot. Adapters must bound the
snapshot before passing it here. Native driver APIs remain explicit escape hatches.
"""
import copy
import functools
import json
import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_ROWS = 10000
MAX_STAGES = 32
MISSING = object()
FORBIDDEN = {'__proto__', 'prototype', 'constructor'}
FILTERS = {'$eq', '$ne', '$gt', '$gte', '$lt', '$lte', '$in', '$nin', '$exists', '$not', '$all', '$size', '$elemMatch', '$contains', '$startsWith'}
EXPRESSIONS = {'$literal', '$ifNull', '$add', '$subtract', '$multiply', '$divide', '$concat', '$toLower', '$toUpper', '$cond', '$eq', '$ne', '$gt', '$gte', '$lt', '$lte', '$and', '$or', '$not', '$in', '$size', '$arrayElemAt', '$concatArrays', '$dateToString'}
ACCUMULATORS = {'$sum', '$avg', '$min', '$max', '$first', '$last', '$push', '$addToSet'}

class QueryError(ValueError):
    def __init__(self, message, code='INVALID_QUERY', status=400):
        super().__init__(message)
        self.code, self.status = code, status


def require(condition, message):
    if not condition:
        raise QueryError(message)


def number(v):
    return isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v)


def canonical(v):
    def normalize(value):
        if number(value) and value == int(value): return int(value)
        if isinstance(value, list): return [normalize(x) for x in value]
        if isinstance(value, dict): return {k: normalize(x) for k, x in value.items()}
        return value
    return json.dumps(normalize(v), sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def equal(a, b):
    if a is MISSING or b is MISSING:
        return a is b
    if number(a) and number(b):
        return a == b
    return canonical(a) == canonical(b)


def field(path):
    require(isinstance(path, str) and 0 < len(path) <= 256, 'Invalid field path')
    require(all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', part) and part not in FORBIDDEN for part in path.split('.')), 'Invalid field path: ' + path)
    return path


def get(row, path):
    parts = path.split('.')
    def walk(value, index):
        if index == len(parts): return value
        if isinstance(value, list):
            found = [item for item in (walk(item, index) for item in value) if item is not MISSING]
            return [v for item in found for v in (item if isinstance(item, list) else [item])] if found else MISSING
        if not isinstance(value, dict) or parts[index] not in value: return MISSING
        return walk(value[parts[index]], index + 1)
    return walk(row, 0)


def scalar_condition(value):
    return isinstance(value, dict) and any(k.startswith('$') and k not in ('$and', '$or', '$nor') for k in value)


def element_matches(item, condition):
    if scalar_condition(condition): return matches({'value': item}, {'value': condition})
    if isinstance(condition, dict): return isinstance(item, dict) and matches(item, condition)
    return equal(item, condition)


def put(row, path, value):
    parts = field(path).split('.')
    target = row
    for part in parts[:-1]:
        require(not isinstance(target.get(part), list), 'Array path writes require explicit array replacement')
        if not isinstance(target.get(part), dict):
            target[part] = {}
        target = target[part]
    if value is MISSING:
        target.pop(parts[-1], None)
    else:
        target[parts[-1]] = copy.deepcopy(value)


def selection(value, parts):
    if not parts: return copy.deepcopy(value)
    if isinstance(value, list):
        require(all(isinstance(item, (dict,list)) for item in value), 'Nested projection requires an array of documents')
        return [selection(item, parts) for item in value]
    if not isinstance(value, dict) or parts[0] not in value: return {}
    return {parts[0]:selection(value[parts[0]],parts[1:])}


def merge_projection(target, source):
    if isinstance(target, dict) and isinstance(source, dict):
        for key,value in source.items(): target[key]=merge_projection(target.get(key),value)
        return target
    if isinstance(target, list) and isinstance(source, list) and len(target)==len(source):
        return [merge_projection(a,b) for a,b in zip(target,source)]
    return copy.deepcopy(source)


def remove_projection(value, parts):
    if isinstance(value,list):
        for item in value: remove_projection(item,parts)
    elif isinstance(value,dict):
        if len(parts)==1: value.pop(parts[0],None)
        elif parts[0] in value: remove_projection(value[parts[0]],parts[1:])


def compare(a, b):
    def rank(v):
        if v is MISSING or v is None: return 0
        if isinstance(v, bool): return 1
        if number(v): return 2
        if isinstance(v, str): return 3
        if isinstance(v, list): return 4
        return 5
    ra, rb = rank(a), rank(b)
    if ra != rb: return (ra > rb) - (ra < rb)
    if ra == 0: return 0
    if ra >= 4: a, b = canonical(a), canonical(b)
    return (a > b) - (a < b)


def validate_value(value, depth=0):
    require(depth <= 32, 'Query nesting exceeds 32 levels')
    if isinstance(value, dict):
        for key, child in value.items():
            require(key not in FORBIDDEN, 'Unsafe object key')
            validate_value(child, depth + 1)
    elif isinstance(value, list):
        require(len(value) <= MAX_ROWS, 'Array exceeds row limit')
        for child in value: validate_value(child, depth + 1)
    elif isinstance(value, float):
        require(math.isfinite(value), 'Non-finite numbers are unsupported')


def validate_filter(query):
    require(isinstance(query, dict), 'Filter must be an object')
    for key, value in query.items():
        if key == '$expr': validate_expression(value)
        elif key in ('$and', '$or', '$nor'):
            require(isinstance(value, list) and len(value) > 0, key + ' needs a nonempty array')
            for q in value: validate_filter(q)
        else:
            field(key)
            if isinstance(value, dict) and any(k.startswith('$') for k in value):
                for op, arg in value.items():
                    require(op in FILTERS, 'Unsupported filter operator: ' + op)
                    if op in ('$in', '$nin', '$all'): require(isinstance(arg, list), op + ' needs an array')
                    if op == '$exists': require(isinstance(arg, bool), '$exists needs a boolean')
                    if op == '$size': require(number(arg) and arg == int(arg) and arg >= 0, '$size needs a nonnegative integer')
                    if op == '$not': validate_filter({key: arg})
                    if op == '$elemMatch': validate_filter({'value': arg} if scalar_condition(arg) else arg)
                    if op in ('$contains', '$startsWith'): search_config(arg)


def search_config(value):
    if isinstance(value, str):
        require(len(value) <= 1024, 'Invalid literal search'); return value, False
    require(isinstance(value, dict) and set(value) <= {'value', 'caseInsensitive'} and isinstance(value.get('value'), str) and isinstance(value.get('caseInsensitive', False), bool), 'Invalid literal search')
    require(len(value['value']) <= 1024, 'Search text exceeds 1024 characters')
    return value['value'], value.get('caseInsensitive', False)


def matches(row, query):
    for key, condition in query.items():
        if key == '$expr':
            if not truth(expression(row, condition)): return False
            continue
        if key in ('$and', '$or', '$nor'):
            results = [matches(row, q) for q in condition]
            if key == '$and' and not all(results): return False
            if key == '$or' and not any(results): return False
            if key == '$nor' and any(results): return False
            continue
        actual = get(row, key)
        def eq(expected):
            return (expected is None and actual is MISSING) or equal(actual, expected) or isinstance(actual, list) and not isinstance(expected, list) and any(equal(v, expected) for v in actual)
        if not isinstance(condition, dict) or not any(k.startswith('$') for k in condition):
            if not eq(condition): return False
            continue
        for op, value in condition.items():
            if op == '$eq' and not eq(value): return False
            if op == '$ne' and eq(value): return False
            if op in ('$gt', '$gte', '$lt', '$lte'):
                def qualifies(item):
                    if not (number(item) and number(value) or isinstance(item, str) and isinstance(value, str)): return False
                    c = compare(item, value)
                    return {'$gt': c > 0, '$gte': c >= 0, '$lt': c < 0, '$lte': c <= 0}[op]
                if not any(qualifies(item) for item in (actual if isinstance(actual, list) else [actual])): return False
            if op == '$in' and not any(eq(v) for v in value): return False
            if op == '$nin' and any(eq(v) for v in value): return False
            if op == '$exists' and (actual is not MISSING) != value: return False
            if op == '$not' and matches(row, {key: value}): return False
            if op == '$size' and (not isinstance(actual, list) or len(actual) != value): return False
            if op == '$all' and (not isinstance(actual, list) or not all(any(equal(a, b) for a in actual) for b in value)): return False
            if op == '$elemMatch' and (not isinstance(actual, list) or not any(element_matches(v, value) for v in actual)): return False
            if op in ('$contains', '$startsWith'):
                needle, insensitive = search_config(value)
                if insensitive: needle = needle.lower()
                def found(item):
                    if not isinstance(item, str): return False
                    if insensitive: item = item.lower()
                    return needle in item if op == '$contains' else item.startswith(needle)
                if not any(found(item) for item in (actual if isinstance(actual, list) else [actual])): return False
    return True


def validate_expression(value):
    if isinstance(value, str) and value.startswith('$'): field(value[1:])
    elif isinstance(value, list):
        for v in value: validate_expression(v)
    elif isinstance(value, dict):
        ops = [k for k in value if k.startswith('$')]
        if ops:
            require(len(value) == 1 and ops[0] in EXPRESSIONS, 'Unsupported expression')
            op = ops[0]; arg = value[op]
            if op == '$literal': return
            if op in ('$eq', '$ne', '$gt', '$gte', '$lt', '$lte', '$in', '$arrayElemAt'): require(isinstance(arg, list) and len(arg) == 2, op + ' needs two arguments')
            if op == '$cond': require(isinstance(arg, list) and len(arg) == 3, '$cond needs [condition, then, else]')
            if op == '$not': require(isinstance(arg, list) and len(arg) == 1, '$not needs one argument')
            if op in ('$and', '$or', '$concatArrays'): require(isinstance(arg, list), op + ' needs an array')
            if op == '$dateToString': require(isinstance(arg, dict) and 'date' in arg and set(arg) <= {'date', 'format', 'timezone', 'onNull'}, 'Invalid date expression')
            if op in ('$ifNull', '$subtract', '$divide'): require(isinstance(arg, list) and len(arg) == 2, op + ' needs two arguments')
            if op in ('$add', '$multiply', '$concat'): require(isinstance(arg, list) and len(arg) > 0, op + ' needs an argument array')
            validate_expression(arg)
        else:
            for k, v in value.items(): field(k); validate_expression(v)


def truth(value): return value is not None and value is not False and not (number(value) and value == 0)


def date_string(arg):
    if arg.get('date') is None: return arg.get('onNull')
    require(isinstance(arg['date'], str), 'Date must be an ISO-8601 string')
    require(re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,3})?(?:Z|[+-][0-9]{2}:[0-9]{2}))?', arg['date']) and not arg['date'].startswith('0000'), 'Invalid ISO date')
    try:
        value = datetime.fromisoformat(arg['date'].replace('Z', '+00:00'))
        if value.tzinfo is None:
            require(len(arg['date']) == 10, 'Date-time requires a timezone')
            value = value.replace(tzinfo=timezone.utc)
        zone = arg.get('timezone', 'UTC')
        require(isinstance(zone, str), 'Timezone must be a string')
        value = value.astimezone(ZoneInfo(zone))
    except (ValueError, ZoneInfoNotFoundError): raise QueryError('Invalid ISO date or timezone')
    fmt = arg.get('format', '%Y-%m-%dT%H:%M:%S.%LZ' if zone in ('UTC', 'Etc/UTC') else '%Y-%m-%dT%H:%M:%S.%L')
    require(isinstance(fmt, str) and len(fmt) <= 256 and not re.search(r'%(?![YmdHMSL%])', re.sub(r'%%', '', fmt)), 'Unsupported date format')
    tokens = {'Y': f'{value.year:04d}', 'm': f'{value.month:02d}', 'd': f'{value.day:02d}', 'H': f'{value.hour:02d}', 'M': f'{value.minute:02d}', 'S': f'{value.second:02d}', 'L': f'{value.microsecond // 1000:03d}', '%': '%'}
    return re.sub(r'%([YmdHMSL%])', lambda match: tokens[match[1]], fmt)


def expression(row, value):
    if isinstance(value, str) and value.startswith('$'):
        found = get(row, value[1:]); return None if found is MISSING else found
    if isinstance(value, list): return [expression(row, v) for v in value]
    if not isinstance(value, dict): return value
    if not any(k.startswith('$') for k in value): return {k: expression(row, v) for k, v in value.items()}
    op, arg = next(iter(value.items()))
    if op == '$literal': return copy.deepcopy(arg)
    if op == '$cond': return expression(row, arg[1] if truth(expression(row, arg[0])) else arg[2])
    if op == '$ifNull':
        first = expression(row, arg[0]); return expression(row, arg[1]) if first is None else first
    if op == '$and': return all(truth(expression(row, item)) for item in arg)
    if op == '$or': return any(truth(expression(row, item)) for item in arg)
    arg = expression(row, arg)
    if op in ('$eq', '$ne'): return equal(arg[0], arg[1]) == (op == '$eq')
    if op in ('$gt', '$gte', '$lt', '$lte'):
        c = compare(arg[0], arg[1]); return {'$gt': c > 0, '$gte': c >= 0, '$lt': c < 0, '$lte': c <= 0}[op]
    if op == '$not': return not truth(arg[0])
    if op == '$in':
        require(isinstance(arg[1], list), '$in requires an array'); return any(equal(arg[0], item) for item in arg[1])
    if op == '$size':
        require(isinstance(arg, list), '$size requires an array'); return len(arg)
    if op == '$arrayElemAt':
        require(isinstance(arg[0], list) and number(arg[1]) and arg[1] == int(arg[1]), 'Invalid array index')
        index = int(arg[1]); return arg[0][index] if -len(arg[0]) <= index < len(arg[0]) else None
    if op == '$concatArrays':
        require(all(isinstance(item, list) for item in arg), '$concatArrays requires arrays'); return [item for items in arg for item in items]
    if op == '$dateToString': return date_string(arg)
    if op in ('$toLower', '$toUpper'):
        require(isinstance(arg, str), op + ' requires a string')
        return arg.lower() if op == '$toLower' else arg.upper()
    if op == '$concat':
        require(all(isinstance(v, str) for v in arg), '$concat requires strings')
        return ''.join(arg)
    require(all(number(v) for v in arg), op + ' requires finite numbers')
    if op == '$add': result = sum(arg)
    elif op == '$multiply': result = math.prod(arg)
    elif op == '$subtract': result = arg[0] - arg[1]
    else:
        require(arg[1] != 0, 'Division by zero'); result = arg[0] / arg[1]
    require(number(result), 'Expression overflow')
    return result


def validate_pipeline(pipeline):
    validate_value(pipeline)
    require(isinstance(pipeline, list) and len(pipeline) <= MAX_STAGES, 'Pipeline must contain at most 32 stages')
    for stage in pipeline:
        require(isinstance(stage, dict) and len(stage) == 1, 'Each stage needs exactly one operator')
        op, value = next(iter(stage.items()))
        if op == '$match': validate_filter(value)
        elif op in ('$skip', '$limit'):
            require(number(value) and value == int(value) and 0 <= value <= MAX_ROWS and (op != '$limit' or value > 0), op + ' is out of range')
        elif op == '$sort':
            require(isinstance(value, dict) and len(value) > 0, '$sort needs fields')
            for k, v in value.items(): field(k); require(number(v) and v in (-1, 1), 'Sort direction must be 1 or -1')
        elif op in ('$project', '$set', '$addFields'):
            require(isinstance(value, dict) and len(value) > 0, op + ' needs fields')
            for k, v in value.items(): field(k); validate_expression(v)
            if op == '$project':
                included = any(not (number(v) and v == 0) for k, v in value.items() if k not in ('id', '_id'))
                excluded = any(number(v) and v == 0 for k, v in value.items() if k not in ('id', '_id'))
                require(not (included and excluded), 'Cannot mix inclusion and exclusion projection')
        elif op == '$unset':
            fields = [value] if isinstance(value, str) else value
            require(isinstance(fields, list) and len(fields) > 0, '$unset needs fields')
            for k in fields: field(k)
        elif op == '$count': field(value)
        elif op == '$group':
            require(isinstance(value, dict) and '_id' in value, '$group needs _id')
            validate_expression(value['_id'])
            for k, v in value.items():
                if k == '_id': continue
                field(k); require(isinstance(v, dict) and len(v) == 1 and next(iter(v)) in ACCUMULATORS, 'Unsupported accumulator')
                validate_expression(next(iter(v.values())))
        elif op == '$unwind':
            if isinstance(value, str): require(value.startswith('$'), '$unwind path must start with $'); field(value[1:])
            else:
                require(isinstance(value, dict) and set(value) <= {'path', 'preserveNullAndEmptyArrays', 'includeArrayIndex'}, 'Invalid $unwind options')
                require(isinstance(value.get('path'), str) and value['path'].startswith('$'), 'Invalid $unwind path'); field(value['path'][1:])
                if 'preserveNullAndEmptyArrays' in value: require(isinstance(value['preserveNullAndEmptyArrays'], bool), 'Invalid preserveNullAndEmptyArrays')
                if 'includeArrayIndex' in value: field(value['includeArrayIndex'])
        else: raise QueryError('Unsupported pipeline stage: ' + op, 'UNSUPPORTED_QUERY')


def byte_budget(rows, stage, max_bytes):
    op, value = next(iter(stage.items()))
    expanding = op in ('$set', '$addFields', '$project', '$group')
    copies = max(1, len(value)) if expanding else 1
    estimate = 0
    for row in rows:
        multiplier = copies
        if op == '$unwind':
            path = value if isinstance(value, str) else value['path']
            items = get(row, path[1:])
            multiplier = max(1, len(items)) if isinstance(items, list) else 1
        estimate += len(json.dumps(row, ensure_ascii=False).encode()) * multiplier
        if estimate > max_bytes: raise QueryError('Query expansion byte budget exceeded', 'QUERY_LIMIT', 413)
    if expanding: estimate += len(json.dumps(value, ensure_ascii=False).encode()) * len(rows)
    if estimate > max_bytes: raise QueryError('Query expansion byte budget exceeded', 'QUERY_LIMIT', 413)


def aggregate(rows, pipeline, max_rows=MAX_ROWS, max_bytes=8388608):
    validate_pipeline(pipeline)
    rows = copy.deepcopy(rows)
    def bound():
        if len(rows) > max_rows: raise QueryError('Query row limit exceeded', 'QUERY_LIMIT', 413)
    bound()
    for stage in pipeline:
        byte_budget(rows, stage, max_bytes)
        op, value = next(iter(stage.items()))
        if op == '$match': rows = [r for r in rows if matches(r, value)]
        elif op == '$skip': rows = rows[int(value):]
        elif op == '$limit': rows = rows[:int(value)]
        elif op == '$sort':
            def cmp(a, b):
                for k, direction in value.items():
                    c = compare(get(a, k), get(b, k)) * direction
                    if c: return c
                return 0
            rows.sort(key=functools.cmp_to_key(cmp))
        elif op == '$count': rows = [{value: len(rows)}] if rows else []
        elif op in ('$set', '$addFields'):
            for row in rows:
                computed = {k: expression(row, v) for k, v in value.items()}
                for k, v in computed.items(): put(row, k, v)
        elif op == '$unset':
            for row in rows:
                for k in ([value] if isinstance(value, str) else value): remove_projection(row, k.split('.'))
        elif op == '$project':
            result = []
            inclusion = any(not (number(v) and v == 0) for v in value.values())
            for row in rows:
                output = {} if inclusion else copy.deepcopy(row)
                if inclusion:
                    for k in ('id', '_id'):
                        if value.get(k) != 0 and k in row: output[k] = row[k]
                for k, v in value.items():
                    if number(v) and v == 0: remove_projection(output,k.split('.'))
                    elif number(v) and v == 1: merge_projection(output,selection(row,k.split('.')))
                    else: put(output, k, expression(row, v))
                result.append(output)
            rows = result
        elif op == '$unwind':
            config = {'path': value} if isinstance(value, str) else value
            path = config['path'][1:]; result = []
            for row in rows:
                values = get(row, path)
                if values is MISSING or values is None or values == []:
                    if config.get('preserveNullAndEmptyArrays'):
                        clone = copy.deepcopy(row)
                        if values == []: put(clone, path, MISSING)
                        if 'includeArrayIndex' in config: put(clone, config['includeArrayIndex'], None)
                        result.append(clone)
                else:
                    array = isinstance(values, list)
                    for i, v in enumerate(values if array else [values]):
                        clone = copy.deepcopy(row); put(clone, path, v)
                        if 'includeArrayIndex' in config: put(clone, config['includeArrayIndex'], i if array else None)
                        result.append(clone)
                        if len(result) > max_rows: raise QueryError('Query expansion exceeds limit', 'QUERY_LIMIT', 413)
            rows = result
        elif op == '$group':
            groups = {}
            for row in rows:
                key = expression(row, value['_id']); serialized = canonical(key)
                if serialized not in groups: groups[serialized] = (key, [])
                groups[serialized][1].append(row)
            result = []
            for key, members in groups.values():
                output = {'_id': key}
                for k, spec in value.items():
                    if k == '_id': continue
                    acc, expr = next(iter(spec.items())); values = [expression(r, expr) for r in members]
                    nums = [v for v in values if number(v)]
                    if acc == '$sum': output[k] = sum(nums)
                    elif acc == '$avg': output[k] = sum(nums) / len(nums) if nums else None
                    elif acc == '$min': output[k] = min(values, key=functools.cmp_to_key(compare))
                    elif acc == '$max': output[k] = max(values, key=functools.cmp_to_key(compare))
                    elif acc == '$first': output[k] = values[0]
                    elif acc == '$last': output[k] = values[-1]
                    elif acc == '$push': output[k] = values
                    else:
                        output[k] = []
                        for v in values:
                            if not any(equal(v, old) for old in output[k]): output[k].append(v)
                result.append(output)
            rows = result
        bound()
    validate_value(rows)
    return rows


def find(rows, query=None, options=None):
    query = {} if query is None else query
    options = {} if options is None else options
    require(isinstance(options, dict) and set(options) <= {'sort', 'skip', 'limit', 'projection'}, 'Unsupported query options')
    pipeline = [{'$match': query}]
    for name in ('sort', 'skip', 'limit', 'projection'):
        if name in options: pipeline.append({'$project' if name == 'projection' else '$' + name: options[name]})
    return aggregate(rows, pipeline)


def updated(current, update):
    """Apply portable update operators to a copy; this is not an atomic DB update."""
    validate_value(update)
    require(isinstance(update, dict) and bool(update), 'Update requires operators')
    result = copy.deepcopy(current)
    paths = []
    for op, changes in update.items():
        require(op in ('$set', '$unset', '$inc', '$push', '$addToSet', '$pull'), 'Unsupported update operator: ' + op)
        require(isinstance(changes, dict), 'Update operator requires an object')
        for path, value in changes.items():
            field(path)
            require(path.split('.')[0] not in ('id', '_id'), 'Record identity cannot be updated')
            require(not any(p == path or p.startswith(path + '.') or path.startswith(p + '.') for p in paths), 'Conflicting update paths')
            paths.append(path)
            previous = get(result, path)
            if op == '$set': put(result, path, value)
            elif op == '$unset': put(result, path, MISSING)
            elif op == '$inc':
                require(number(value) and (previous is MISSING or number(previous)), '$inc requires numbers')
                next_value = (0 if previous is MISSING else previous) + value
                require(number(next_value), 'Update overflow')
                put(result, path, next_value)
            else:
                items = [] if previous is MISSING else previous
                require(isinstance(items, list), op + ' requires an array field')
                if op in ('$push', '$addToSet'):
                    additions = [value]
                    if isinstance(value, dict) and any(k.startswith('$') for k in value):
                        require(set(value) == {'$each'} and isinstance(value['$each'], list), 'Only the $each array modifier is supported')
                        additions = value['$each']
                    for addition in additions:
                        if op == '$push' or not any(equal(addition, item) for item in items): items = [*items, addition]
                else:
                    if isinstance(value, dict): validate_filter({'value': value} if scalar_condition(value) else value)
                    items = [item for item in items if not element_matches(item, value)]
                put(result, path, items)
    return result
