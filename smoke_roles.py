"""Manifest-driven live role/session checks, including direct native replicas."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor


def run_checks(
    manifest,
    env,
    request,
    check,
    assert_,
    cmd,
    compose,
    admin,
    sample,
    health,
    verify_persistence,
):
    routes = [(s, r) for s in manifest["services"] for r in s["routes"]]
    suffix = secrets.token_hex(5)
    password = "Smoke-password-" + secrets.token_hex(12)

    def expect(result, status, label):
        assert_(
            result[0] == status,
            f"{label}: expected {status}, got {result[0]} {str(result[1])[:180]}",
        )
        return result[1]

    def manage(path, method="GET", body=None):
        return request(path, method, body, admin)

    def role(name, scopes):
        return expect(
            manage(
                "/auth/roles", "POST", {"name": name + "-" + suffix, "scopes": scopes}
            ),
            201,
            "Create role",
        )

    created_accounts = []

    def account(name, r):
        result = expect(
            manage(
                "/auth/accounts",
                "POST",
                {
                    "username": name + "-" + suffix,
                    "password": password,
                    "role_id": r["id"],
                },
            ),
            201,
            "Create account",
        )
        created_accounts.append(result["id"])
        return result

    def login(a, p=password):
        return expect(
            request("/auth/login", "POST", {"username": a["username"], "password": p}),
            200,
            "Account login",
        )

    def grant(r, scopes):
        return expect(
            manage(
                "/auth/roles/" + r["id"], "PUT", {"name": r["name"], "scopes": scopes}
            ),
            200,
            "Change role permissions",
        )

    def assign(a, r, enabled=True, **fields):
        return expect(
            manage(
                "/auth/accounts/" + a["id"],
                "PUT",
                {"role_id": r["id"], "enabled": enabled, **fields},
            ),
            200,
            "Assign account role",
        )

    def direct(c, path, method, t, data=None):
        address = cmd(compose + ["port", c, "8000"]).strip().splitlines()[0]
        return request(path, method, data, t, origin="http://" + address)

    state = {}

    def contract():
        me = expect(manage("/auth/me"), 200, "Admin identity")
        assert_(
            me["admin"] and me["role"]["system"],
            "Bootstrap administrator not authoritative",
        )
        state["me"] = me
        expect(request("/auth/roles"), 401, "Anonymous role management")
        expect(
            manage(
                "/auth/roles/" + me["role"]["id"],
                "PUT",
                {"name": "overwrite", "scopes": []},
            ),
            409,
            "Protected role update",
        )
        expect(
            manage("/auth/roles/" + me["role"]["id"], "DELETE"),
            409,
            "Protected role deletion",
        )
        expect(
            manage("/auth/accounts/" + me["account"]["id"], "DELETE"),
            409,
            "Last administrator deletion",
        )
        for scopes in [["*"], ["missing:model:read"], "read", None]:
            expect(
                manage("/auth/roles", "POST", {"name": "invalid", "scopes": scopes}),
                400,
                "Invalid scope definition",
            )
        expect(
            manage(
                "/auth/roles",
                "POST",
                {"name": "escalate", "scopes": [], "system": True},
            ),
            400,
            "Role privilege mass assignment",
        )
        schemas = expect(request("/api/docs.json"), 200, "Identity OpenAPI")[
            "components"
        ]["schemas"]
        assert_(
            "HuzaRole" in schemas and "HuzaAccount" in schemas,
            "Missing identity schemas",
        )

    check(
        "JWT roles contract, deny defaults and protected last administrator", contract
    )
    for svc, route in routes:

        def model(svc=svc, route=route):
            prefix = svc["name"] + ":" + route["module"] + ":"
            path = route["list"]
            local = "/" + route["module"] + "/"
            r = role(svc["name"] + "-" + route["module"], [])
            a = account(svc["name"] + "-" + route["module"], r)
            tokens = login(a)
            token = tokens["access_token"]
            claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
            assert_(
                claims["sub"] == a["id"]
                and claims["role_id"] == r["id"]
                and claims["role"] == r["name"]
                and "version" not in claims
                and "scopes" not in claims,
                "Wrong role claims",
            )
            expect(request(path, token=token), 403, "Empty role denies")
            for management in [
                "/auth/roles",
                "/auth/accounts",
                "/auth/api-keys",
                "/auth/scopes",
            ]:
                expect(request(management, token=token), 403, "Non-admin management")
            grant(r, [prefix + "read"])
            expect(request(path, token=token), 200, "Same JWT gains read")
            data = sample(route["schema"], "role-" + str(time.time_ns()))
            seed = expect(request(path, "POST", data, admin), 201, "Admin seed")
            item = path + seed["id"]
            expect(request(item, token=token), 200, "Role detail")
            query_path = path.rstrip('/') + '/_query'
            aggregate_path = path.rstrip('/') + '/_aggregate'
            expect(request(query_path, 'POST', {'filter': {}}, token), 200, 'Read role permits query POST')
            expect(request(aggregate_path, 'POST', {'pipeline': [{'$limit': 1}]}, token), 200, 'Read role permits aggregation POST')
            grant(r, [prefix + 'create'])
            expect(request(query_path, 'POST', {'filter': {}}, token), 403, 'Role read revocation immediately denies query')
            for c in svc['containers']:
                expect(direct(c, local.rstrip('/') + '/_aggregate', 'POST', token, {'pipeline': []}), 403, 'Role read revocation denies direct aggregation')
            grant(r, [prefix + 'read'])
            expect(request(query_path, 'POST', {'filter': {}}, token), 200, 'Same token observes restored read permission')
            for method, url in [("POST", path), ("PUT", item), ("DELETE", item)]:
                expect(
                    request(url, method, data if method != "DELETE" else None, token),
                    403,
                    "Read role cannot " + method,
                )
            for other_s, other_r in routes:
                if (other_s["name"], other_r["module"]) != (
                    svc["name"],
                    route["module"],
                ):
                    expect(
                        request(other_r["list"], token=token),
                        403,
                        "Cross-model role isolation",
                    )
            for c in svc["containers"]:
                expect(direct(c, local, "GET", token), 200, "Direct role read")
                expect(
                    direct(c, local, "POST", token, data), 403, "Direct write denied"
                )
            grant(r, [prefix + x for x in ("read", "create", "update", "delete")])
            created = expect(
                request(path, "POST", data, token), 201, "Expanded role create"
            )
            target = path + created["id"]
            verify_persistence(svc, route, created["id"], data)
            changed = sample(route["schema"], "role-edit-" + str(time.time_ns()))
            expect(request(target, "PUT", changed, token), 200, "Role update")
            actual = expect(request(target, token=token), 200, "Updated role detail")
            assert_(
                all(actual[k] == v for k, v in changed.items()), "Role update payload"
            )
            expect(request(target, "DELETE", token=token), 200, "Role delete")
            expect(request(target, token=token), 404, "Role deleted record")
            grant(r, [prefix + "read"])
            for c in svc["containers"]:
                expect(
                    direct(c, local, "POST", token, data),
                    403,
                    "Immediate role reduction at replica",
                )
            seen = set()
            for _ in range(2 * svc["instances"]):
                result = request(path, token=token)
                expect(result, 200, "Role load balancing")
                seen.add(result[2].get("X-Instance-ID"))
            assert_(
                None not in seen and len(seen) == svc["instances"],
                "JWT role load balancing",
            )
            expect(
                manage("/auth/roles/" + r["id"], "DELETE"),
                409,
                "Assigned role deletion",
            )
            other = role(
                "alternate-" + svc["name"] + "-" + route["module"], [prefix + "read"]
            )
            assign(a, other)
            expect(
                request(path, token=token), 401, "Role reassignment invalidates access"
            )
            for c in svc["containers"]:
                expect(
                    direct(c, local, "GET", token),
                    401,
                    "Reassignment invalidates all replicas",
                )
            assign(a, r)
            expect(
                request(path, token=token),
                401,
                "Role reassignment back cannot resurrect old JWT",
            )
            assign(a, other)
            fresh = expect(
                request(
                    "/auth/refresh", "POST", {"refresh_token": tokens["refresh_token"]}
                ),
                200,
                "Refresh adopts current role",
            )
            fresh_claims = json.loads(
                base64.urlsafe_b64decode(fresh["access_token"].split(".")[1] + "==")
            )
            assert_(
                fresh_claims["role_id"] == other["id"], "Refresh kept obsolete role"
            )
            expect(
                request(path, token=fresh["access_token"]),
                200,
                "New role token accepted",
            )
            expect(
                request(
                    "/auth/refresh", "POST", {"refresh_token": tokens["refresh_token"]}
                ),
                401,
                "Refresh replay rejected",
            )
            expect(
                request(path, token=fresh["access_token"]),
                401,
                "Replay revokes family access",
            )
            expect(
                request(
                    "/auth/refresh", "POST", {"refresh_token": fresh["refresh_token"]}
                ),
                401,
                "Replay revokes family refresh",
            )
            expect(request(item, "DELETE", token=admin), 200, "Role seed cleanup")

        check(
            f"JWT role {svc['lang']}×{svc['database']} {svc['name']}/{route['module']} live CRUD, isolation, reassignment and replay",
            model,
        )

    def sessions():
        svc, route = routes[0]
        scope = svc["name"] + ":" + route["module"] + ":read"
        r = role("sessions", [scope])
        a = account("sessions", r)
        t = login(a)
        assign(a, r, False)
        expect(request("/auth/me", token=t["access_token"]), 401, "Disabled access")
        expect(
            request("/auth/refresh", "POST", {"refresh_token": t["refresh_token"]}),
            401,
            "Disabled refresh",
        )
        expect(
            request(
                "/auth/login", "POST", {"username": a["username"], "password": password}
            ),
            401,
            "Disabled login",
        )
        assign(a, r)
        expect(
            request("/auth/me", token=t["access_token"]),
            401,
            "Reenable cannot resurrect access",
        )
        expect(
            request("/auth/refresh", "POST", {"refresh_token": t["refresh_token"]}),
            401,
            "Reenable cannot resurrect refresh",
        )
        t = login(a)
        changed = password + "-reset"
        assign(a, r, password=changed)
        expect(
            request("/auth/me", token=t["access_token"]),
            401,
            "Password reset access revocation",
        )
        expect(
            request("/auth/refresh", "POST", {"refresh_token": t["refresh_token"]}),
            401,
            "Password reset refresh revocation",
        )
        t = login(a, changed)
        expect(
            request("/auth/logout", "POST", {"refresh_token": t["refresh_token"]}),
            200,
            "Logout",
        )
        expect(
            request("/auth/me", token=t["access_token"]), 401, "Logout revokes access"
        )
        expect(
            request("/auth/refresh", "POST", {"refresh_token": t["refresh_token"]}),
            401,
            "Logout revokes refresh",
        )
        t = login(a, changed)
        bad = t["refresh_token"][:-1] + ("0" if t["refresh_token"][-1] != "0" else "1")
        expect(
            request("/auth/refresh", "POST", {"refresh_token": bad}),
            401,
            "Forged refresh",
        )
        expect(
            request("/auth/me", token=t["access_token"]),
            200,
            "Random wrong secret cannot revoke valid family",
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda _: request(
                        "/auth/refresh", "POST", {"refresh_token": t["refresh_token"]}
                    ),
                    range(2),
                )
            )
        assert_(
            sorted(x[0] for x in responses) == [200, 401],
            "Refresh consumption must be atomic",
        )
        winner = next(x[1] for x in responses if x[0] == 200)
        expect(
            request("/auth/me", token=winner["access_token"]),
            401,
            "Concurrent replay revoked family",
        )
        # Inspect persisted hashes without copying raw account or session secrets to reports.
        inspection = "import sqlite3,json; c=sqlite3.connect('/data/auth.sqlite');print(json.dumps({'hashes':[x[0].startswith('$argon2id$v=19$m=65536,t=3,p=1$') for x in c.execute('SELECT password_hash FROM accounts')],'refresh':[len(x[0])==64 for x in c.execute('SELECT refresh_hash FROM sessions')]}))"
        stored = json.loads(
            cmd(compose + ["exec", "-T", "identity", "python", "-c", inspection])
        )
        assert_(
            all(stored["hashes"]) and all(stored["refresh"]),
            "Password/refresh storage does not use expected hashes",
        )

    check(
        "JWT sessions disable, password reset, logout, atomic rotation and hash storage",
        sessions,
    )

    def forged():
        svc, route = routes[0]
        r = role("claims", [])
        a = account("claims", r)
        t = login(a)
        claims = json.loads(
            base64.urlsafe_b64decode(t["access_token"].split(".")[1] + "==")
        )

        def sign(c):
            enc = lambda d: base64.urlsafe_b64encode(
                json.dumps(d, separators=(",", ":")).encode()
            ).rstrip(b"=")
            raw = enc({"alg": "HS256", "typ": "JWT"}) + b"." + enc(c)
            return (
                raw
                + b"."
                + base64.urlsafe_b64encode(
                    hmac.new(env["JWT_SECRET"].encode(), raw, hashlib.sha256).digest()
                ).rstrip(b"=")
            ).decode()

        for changed in [
            {"iss": "wrong"},
            {"aud": "wrong"},
            {"kind": "refresh"},
            {"role_id": state["me"]["role"]["id"]},
            {"sub": state["me"]["account"]["id"]},
            {"jti": "wrong"},
            {"sid": "wrong"},
            {"exp": 1},
        ]:
            expect(
                request(route["list"], token=sign({**claims, **changed})),
                401,
                "Reject changed token claim",
            )
        expect(
            request("/auth/api-keys", token=sign({**claims, "role": "admin"})),
            403,
            "Role name claim cannot elevate privileges",
        )
        for _ in range(5):
            expect(
                request(
                    "/auth/login",
                    "POST",
                    {"username": "missing-" + suffix, "password": password},
                ),
                401,
                "Invalid login",
            )
        expect(
            request(
                "/auth/login",
                "POST",
                {"username": "missing-" + suffix, "password": password},
            ),
            429,
            "Login throttled",
        )

    check(
        "JWT issuer, audience, session binding, claim tampering and login throttle",
        forged,
    )

    def persistence():
        svc, route = routes[0]
        r = role("persistent", [svc["name"] + ":" + route["module"] + ":read"])
        a = account("persistent", r)
        t = login(a)
        try:
            cmd(compose + ["stop", "identity"], timeout=90)
            expect(
                request(route["list"], token=t["access_token"]),
                503,
                "Gateway fails closed",
            )
            for service, rt in routes:
                for c in service["containers"]:
                    expect(
                        direct(c, "/" + rt["module"] + "/", "GET", t["access_token"]),
                        503,
                        "Direct native replica fails closed",
                    )
        finally:
            cmd(compose + ["start", "identity"], timeout=90)
            for _ in range(60):
                if request("/auth/me", token=admin)[0] == 200:
                    break
                time.sleep(1)
        expect(
            request(route["list"], token=t["access_token"]),
            200,
            "Identity persists on restart",
        )
        for service, rt in routes:
            expect(request(rt['list'], token=admin), 200, 'Every service recovers after authority restart')
            for c in service['containers']:
                expect(direct(c, '/' + rt['module'] + '/_query', 'POST', admin, {'filter': {}}), 200, 'Every replica query recovers after authority restart')
        grant(r, [])
        expect(
            request(route["list"], token=t["access_token"]),
            403,
            "Live permission change after restart",
        )

    check(
        "JWT authority outage fails closed and accounts, roles, sessions survive restart",
        persistence,
    )

    def cleanup():
        for account_id in created_accounts:
            result = expect(
                manage("/auth/accounts/" + account_id, "DELETE"),
                200,
                "Disable smoke fixture",
            )
            assert_(result["enabled"] is False, "Smoke fixture account remains enabled")

    check("JWT smoke accounts disabled after validation", cleanup)
