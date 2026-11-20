"""Manifest-driven portable query and schema checks using real generated routes."""
import uuid


def run_checks(manifest, request, check, assert_, token, sample, cmd, compose):
    def expect(result, status, label):
        assert_(result[0] == status, f'{label}: expected {status}, got {result[0]}: {result[1]}')
        return result[1]

    for service in manifest['services']:
        for route in service['routes']:
            def workflow(service=service, route=route):
                base = route['list'].rstrip('/')
                module = route['module']
                rows, integration = [], None
                try:
                    for i in range(3):
                        rows.append(expect(request(route['list'], 'POST', sample(route['schema'], 'query-'+uuid.uuid4().hex[:12]), token), 201, 'Create query fixture'))
                    for field, spec in route['schema'].get('properties', {}).items():
                        if spec.get('type') == 'object' and spec.get('additionalProperties', True) is not False:
                            unsafe = sample(route['schema'], 'unsafe-query-record')
                            unsafe[field] = {'__proto__': {'polluted': True}}
                            response = request(route['list'], 'POST', unsafe, token)
                            if response[0] == 201: rows.append(response[1])
                            assert_(response[0] in (400, 422), 'Unsafe nested keys must be rejected before storing an unqueryable record')
                    ids = sorted(row['id'] for row in rows)
                    condition = {'id': {'$in': ids}}
                    query = {'filter': condition, 'options': {'sort': {'id': 1}, 'skip': 1, 'limit': 1, 'projection': {'id': 1}}}
                    expect(request(base+'/_query', 'POST', query), 401, 'Anonymous query')
                    found = expect(request(base+'/_query', 'POST', query, token), 200, 'Filter/sort/pagination/projection')
                    assert_(found == [{'id': ids[1]}], 'Portable query result mismatch')
                    pipeline = [
                        {'$match': condition}, {'$sort': {'id': 1}},
                        {'$set': {'probe': {'$literal': [2, 4]}}}, {'$unwind': '$probe'},
                        {'$group': {'_id': None, 'count': {'$sum': 1}, 'total': {'$sum': '$probe'}, 'average': {'$avg': '$probe'},
                                    'minimum': {'$min': '$probe'}, 'maximum': {'$max': '$probe'}, 'first': {'$first': '$id'}, 'last': {'$last': '$id'}, 'ids': {'$addToSet': '$id'}}},
                        {'$project': {'_id': 0, 'count': 1, 'total': 1, 'average': 1, 'minimum': 1, 'maximum': 1, 'first': 1, 'last': 1, 'ids': 1}}
                    ]
                    result = expect(request(base+'/_aggregate', 'POST', {'pipeline': pipeline}, token), 200, 'Portable aggregation')
                    assert_(result == [{'count': 6, 'total': 18, 'average': 3, 'minimum': 2, 'maximum': 4, 'first': ids[0], 'last': ids[-1], 'ids': ids}], 'Aggregation semantics differ across runtimes')
                    expect(request(base+'/_aggregate', 'POST', {'pipeline': [{'$match': {'id': 'absent'}}, {'$unknown': {}}]}, token), 400, 'Unsupported stage rejected on empty data')
                    expect(request(base+'/_query', 'POST', {'filter': {'id': {'$where': 'unsafe'}}}, token), 400, 'Unknown operator rejected')
                    for body in ({'options': {'limit': -1}}, {'filter': []}, {'options': None}):
                        expect(request(base+'/_query', 'POST', body, token), 400, 'Malformed query rejected')
                    bad = request(route['list'], 'POST', [], token)
                    assert_(bad[0] in (400, 422), 'Non-object record was accepted')
                    name = f"{service['name']}:{module}"
                    integration = expect(request('/auth/api-keys', 'POST', {'name': 'Query read scope', 'scopes': [name+':create'], 'expiresAt': None}, token), 201, 'Create limited key')
                    expect(request(base+'/_aggregate', 'POST', {'pipeline': pipeline}, api_key=integration['key']), 403, 'Create permission cannot aggregate')
                    expect(request('/auth/api-keys/'+integration['id'], 'PUT', {'name': 'Query read scope', 'scopes': [name+':read'], 'expiresAt': None}, token), 200, 'Enable read with same key')
                    expect(request(base+'/_aggregate', 'POST', {'pipeline': pipeline}, api_key=integration['key']), 200, 'Read permission enables aggregation')
                    for container in service['containers']:
                        address = cmd(compose+['port', container, '8000']).strip().splitlines()[0]
                        expect(request('/'+module+'/_query', 'POST', query, api_key=integration['key'], origin='http://'+address), 200, 'Direct replica query read scope')
                    expect(request('/auth/api-keys/'+integration['id'], 'PUT', {'name': 'Query read scope', 'scopes': [], 'expiresAt': None}, token), 200, 'Remove read scope')
                    expect(request(base+'/_query', 'POST', query, api_key=integration['key']), 403, 'Live scope removal applies to query')
                finally:
                    for row in rows:
                        expect(request(route['detail'].replace('{id}', row['id']), 'DELETE', token=token), 200, 'Remove query fixture')
                    if integration: expect(request('/auth/api-keys/'+integration['id'], 'DELETE', token=token), 200, 'Revoke query key')
            check(f"Shared queries {service['lang']}×{service['database']} {service['name']}/{route['module']}: aggregation, filters, projection, pagination, validation and live read scopes", workflow)
