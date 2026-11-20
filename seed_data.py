#!/usr/bin/env python3
"""Seed fresh placeholder models through the authenticated gateway, once per empty collection."""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import fcntl  # Linux / macOS
except ImportError:  # Windows has no fcntl
    fcntl = None
    import msvcrt


def acquire_lock(lock):
    """Take a non-blocking exclusive lock; raise BlockingIOError if another run holds it."""
    if fcntl is not None:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as error:
        raise BlockingIOError(str(error)) from None


class Gateway:
    def __init__(self, base):
        self.base, self.token = base, None

    def request(self, path, method='GET', data=None):
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
                                         data=json.dumps(data).encode() if data is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read()
                return response.status, json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'{method} {path}: HTTP {error.code}') from None
        except (OSError, ValueError):
            raise RuntimeError(f'{method} {path}: gateway unavailable or invalid response') from None

    def wait_ready(self, timeout=360):
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.request('/health')[0] == 200:
                    return
            except RuntimeError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('Gateway did not become ready for seeding')
            time.sleep(1)


def seed_records(manifest, plan, gateway):
    routes = {(service['name'], route['module']): route['list']
              for service in manifest['services'] for route in service['routes']}
    seen, results = set(), []
    # Validate the whole plan before sending any writes.
    for item in plan['records']:
        key = item['service'], item['model']
        if key not in routes or key in seen:
            raise ValueError(f'Unknown or duplicate seed target: {key}')
        if item['data'] is not None and (not isinstance(item['data'], dict) or
                                        {'id', '_id'} & item['data'].keys()):
            raise ValueError(f'{key}: seed data must be an object without reserved record IDs')
        seen.add(key)
    for item in plan['records']:
        key = item['service'], item['model']
        result = {'service': key[0], 'model': key[1]}
        if item['data'] is None:
            results.append({**result, 'status': 'skipped', 'reason': 'custom schema: no explicit sample supplied'})
            continue
        try:
            path = routes[key]
            status, existing = gateway.request(path)
            if status != 200 or not isinstance(existing, list):
                raise RuntimeError('Collection did not return a valid list')
            if existing:
                results.append({**result, 'status': 'skipped', 'reason': 'collection already contains records'})
                continue
            status, created = gateway.request(path, 'POST', item['data'])
            if status != 201 or not isinstance(created, dict) or not isinstance(created.get('id'), str) or not created['id']:
                raise RuntimeError('Create did not return a record ID with HTTP 201')
            status, saved = gateway.request(path + urllib.parse.quote(created['id'], safe=''))
            if status != 200 or not isinstance(saved, dict) or any(saved.get(k) != v for k, v in item['data'].items()):
                raise RuntimeError('Stored record does not match the generated sample')
            results.append({**result, 'status': 'created', 'id': created['id']})
        except (RuntimeError, ValueError) as error:
            results.append({**result, 'status': 'failed', 'reason': str(error)})
    return results


def run(root):
    manifest = json.loads((root / '.huzapilot.json').read_text())
    report = {'enabled': manifest.get('seeding', {}).get('enabled', False), 'results': []}
    refresh, gateway = None, None
    try:
        if report['enabled']:
            plan = json.loads((root / 'seed-data.json').read_text())
            if any(item['data'] is not None for item in plan['records']):
                env = {}
                for line in (root / '.env').read_text().splitlines():
                    if '=' in line and not line.lstrip().startswith('#'):
                        key, value = line.split('=', 1)
                        value = value.strip()
                        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                            value = value[1:-1]
                        env[key.strip()] = value
                env.update(os.environ)
                gateway = Gateway('http://127.0.0.1:' + str(int(env.get('GATEWAY_PORT', '8080'))))
                gateway.wait_ready()
                status, session = gateway.request('/auth/login', 'POST', {
                    'username': env['AUTH_USERNAME'], 'password': env['AUTH_PASSWORD']})
                if status != 200 or not isinstance(session, dict) or not session.get('access_token') or not session.get('refresh_token'):
                    raise RuntimeError('Seed login did not return a complete session')
                gateway.token, refresh = session['access_token'], session['refresh_token']
            report['results'] = seed_records(manifest, plan, gateway)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        report['error'] = str(error)
    finally:
        if refresh:
            try:
                gateway.request('/auth/logout', 'POST', {'refresh_token': refresh})
            except RuntimeError as error:
                report['error'] = 'Seed session cleanup failed: ' + str(error)
    for status in ('created', 'skipped', 'failed'):
        report[status] = sum(item['status'] == status for item in report['results'])
    report['success'] = not report.get('error') and report['failed'] == 0
    (root / 'seed-results.json').write_text(json.dumps(report, indent=2) + '\n')
    if not report['enabled']:
        print('Sample seeding disabled (--no-seed).')
    for item in report['results']:
        print(f"{item['status'].upper()} {item['service']}/{item['model']}: {item.get('reason', 'sample persisted')}")
    if report.get('error'):
        print(report['error'], file=sys.stderr)
    print(f"Seeding: {report['created']} created, {report['skipped']} skipped, {report['failed']} failed.")
    return 0 if report['success'] else 1


def main():
    root = Path(__file__).resolve().parent
    # One coordinator per generated project; never one writer per service replica.
    with (root / '.seed-data.lock').open('a') as lock:
        try:
            acquire_lock(lock)
        except BlockingIOError:
            print('Another seed run is active for this project.', file=sys.stderr)
            return 1
        return run(root)


if __name__ == '__main__':
    sys.exit(main())
