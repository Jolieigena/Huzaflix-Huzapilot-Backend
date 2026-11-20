"""Live account/role authorization and revocable, rotating sessions."""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from mailer import Mailer

SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS roles(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,scopes TEXT NOT NULL,system INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,role_id TEXT NOT NULL REFERENCES roles(id),enabled INTEGER NOT NULL DEFAULT 1,email TEXT);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),refresh_hash TEXT NOT NULL,used_hashes TEXT NOT NULL,access_id TEXT NOT NULL,expires_at INTEGER NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account_id);
CREATE TABLE IF NOT EXISTS password_resets(id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),digest TEXT NOT NULL UNIQUE,expires_at INTEGER NOT NULL,created_at INTEGER NOT NULL,issued_by TEXT);
CREATE INDEX IF NOT EXISTS password_resets_account ON password_resets(account_id);
"""


class IdentityError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Identity:
    def __init__(self, scopes):
        self.catalog = set(scopes)
        self.lock = threading.RLock()
        self.failures = {}
        self.db = sqlite3.connect(
            os.getenv("IDENTITY_DB", "/data/auth.sqlite"),
            check_same_thread=False,
            timeout=10,
        )
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Databases created before password reset existed have no email column.
        if "email" not in {r["name"] for r in self.db.execute("PRAGMA table_info(accounts)")}:
            self.db.execute("ALTER TABLE accounts ADD COLUMN email TEXT")
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS accounts_email ON accounts(email) WHERE email IS NOT NULL"
        )
        self.hits = {}
        self.reset_mode = os.getenv("PASSWORD_RESET", "off").strip().lower() or "off"
        if self.reset_mode not in ("off", "admin", "email"):
            raise ValueError("PASSWORD_RESET must be off, admin or email")
        self.reset_url = os.getenv("PASSWORD_RESET_URL", "").strip()
        self.mailer = Mailer.from_env() if self.reset_mode == "email" else None
        if self.reset_mode == "email" and not (self.mailer and self.reset_url):
            logging.error(
                "PASSWORD_RESET=email but SMTP_HOST, SMTP_FROM or PASSWORD_RESET_URL is missing; reset emails cannot be sent"
            )
        self.hasher = PasswordHasher(
            time_cost=3, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16
        )
        self.dummy = self.hasher.hash(secrets.token_hex(32))
        self.secret = os.environ["JWT_SECRET"]
        if len(self.secret) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters")
        self.issuer = os.environ["JWT_ISSUER"]
        self.audience = "huza-api"
        with self.db:
            if not self.one("SELECT id FROM accounts LIMIT 1"):
                role = str(uuid.uuid4())
                self.db.execute(
                    "INSERT INTO roles VALUES(?,?,?,1)", (role, "Administrator", "[]")
                )
                username = self.username(os.environ["AUTH_USERNAME"])
                password = self.password(os.environ["AUTH_PASSWORD"])
                admin_email = os.getenv("AUTH_EMAIL", "").strip()
                self.db.execute(
                    "INSERT INTO accounts(id,username,password_hash,role_id,enabled,email) VALUES(?,?,?,?,1,?)",
                    (
                        str(uuid.uuid4()),
                        username,
                        self.hasher.hash(password),
                        role,
                        self.email(admin_email) if admin_email else None,
                    ),
                )
            # An administrator email added to .env later is applied once to the bootstrap account.
            admin_email = os.getenv("AUTH_EMAIL", "").strip()
            if admin_email:
                self.db.execute(
                    "UPDATE accounts SET email=? WHERE username=? AND email IS NULL",
                    (self.email(admin_email), self.username(os.environ["AUTH_USERNAME"])),
                )

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def username(self, v):
        if not isinstance(v, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9@._+\-]{2,127}", v
        ):
            raise IdentityError(400, "Username must contain 3–128 supported characters")
        return v.lower()

    def password(self, v):
        if not isinstance(v, str) or not 15 <= len(v) <= 128:
            raise IdentityError(400, "Password must contain 15–128 characters")
        return v

    def email(self, v):
        v = v.strip().lower() if isinstance(v, str) else v
        if (
            not isinstance(v, str)
            or len(v) > 254
            or not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s.]{2,}", v)
        ):
            raise IdentityError(400, "A valid email address is required")
        return v

    def limited(self, key, limit, window):
        """True once `key` has made `limit` attempts inside `window` seconds."""
        now = time.time()
        if len(self.hits) > 20000:
            self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < window}
        recent = [t for t in self.hits.get(key, []) if now - t < window]
        if len(recent) >= limit:
            self.hits[key] = recent
            return True
        recent.append(now)
        self.hits[key] = recent
        return False

    def new_reset(self, account_id, lifetime, issued_by):
        """Create a single-use token; any earlier token for the account stops working."""
        token = secrets.token_hex(32)
        now = int(time.time())
        self.db.execute("DELETE FROM password_resets WHERE account_id=?", (account_id,))
        self.db.execute(
            "INSERT INTO password_resets VALUES(?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                account_id,
                hashlib.sha256(token.encode()).hexdigest(),
                now + lifetime,
                now,
                issued_by,
            ),
        )
        return token

    def reset_link(self, token):
        return self.reset_url + ("&" if "?" in self.reset_url else "?") + "token=" + token

    def forgot_password(self, b, peer):
        """Always answers the same, so it never reveals whether an address has an account."""
        if self.reset_mode != "email":
            raise IdentityError(404, "Not found")
        answer = (200, {"received": True})
        email = self.email(b.get("email"))
        if self.limited("forgot-peer|" + peer, 10, 900):
            raise IdentityError(429, "Too many requests; retry later")
        if self.limited("forgot-email|" + email, 3, 900):
            return answer
        a = self.one("SELECT * FROM accounts WHERE email=?", (email,))
        if a and a["enabled"]:
            token = self.new_reset(a["id"], 3600, None)
            if self.mailer and self.reset_url:
                text = (
                    "Someone asked to reset the password for your account.\n\n"
                    "Open this link to choose a new password. It works once and expires in 1 hour:\n"
                    + self.reset_link(token)
                    + "\n\nIf you did not ask for this, you can ignore this email. Your password has not changed.\n"
                )
                threading.Thread(
                    target=self.mailer.send_quietly,
                    args=(email, "Reset your password", text),
                    daemon=True,
                ).start()
            else:
                logging.error("password reset requested but email is not configured")
        return answer

    def reset_password(self, b, peer):
        if self.reset_mode == "off":
            raise IdentityError(404, "Not found")
        if self.limited("reset|" + peer, 20, 900):
            raise IdentityError(429, "Too many requests; retry later")
        token = b.get("token")
        bad = IdentityError(400, "This link is not valid or has expired. Ask for a new one.")
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
            raise bad
        password = self.password(b.get("password"))
        row = self.one(
            "SELECT * FROM password_resets WHERE digest=? AND expires_at>?",
            (hashlib.sha256(token.encode()).hexdigest(), int(time.time())),
        )
        a = self.one("SELECT * FROM accounts WHERE id=?", (row["account_id"],)) if row else None
        if not a or not a["enabled"]:
            raise bad
        self.db.execute(
            "UPDATE accounts SET password_hash=? WHERE id=?",
            (self.hasher.hash(password), a["id"]),
        )
        self.db.execute("UPDATE sessions SET revoked=1 WHERE account_id=?", (a["id"],))
        self.db.execute("DELETE FROM password_resets WHERE account_id=?", (a["id"],))
        return 200, {"reset": True}

    def public_account(self, a):
        return {
            k: (bool(v) if k == "enabled" else v)
            for k, v in a.items()
            if k != "password_hash"
        }

    def public_role(self, r):
        return {
            **r,
            "scopes": sorted(self.catalog) if r["system"] else json.loads(r["scopes"]),
            "system": bool(r["system"]),
        }

    def claims(self, token):
        try:
            c = jwt.decode(
                token,
                self.secret,
                algorithms=["HS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={
                    "require": [
                        "exp",
                        "iat",
                        "iss",
                        "aud",
                        "sub",
                        "sid",
                        "jti",
                        "role_id",
                        "kind",
                    ]
                },
            )
            if c["kind"] != "access":
                raise ValueError()
            return c
        except (ValueError, jwt.PyJWTError):
            raise IdentityError(401, "Invalid session")

    def actor(self, token):
        c = self.claims(token)
        a = self.one("SELECT * FROM accounts WHERE id=?", (c["sub"],))
        s = self.one("SELECT * FROM sessions WHERE id=?", (c["sid"],))
        if (
            not a
            or not a["enabled"]
            or not s
            or s["revoked"]
            or s["account_id"] != a["id"]
            or s["expires_at"] <= time.time()
            or s["access_id"] != c["jti"]
            or a["role_id"] != c["role_id"]
        ):
            raise IdentityError(401, "Invalid session")
        r = self.one("SELECT * FROM roles WHERE id=?", (a["role_id"],))
        if not r:
            raise IdentityError(401, "Invalid session")
        return a, r, s

    def authorize(self, token, scope):
        with self.lock:
            a, r, s = self.actor(token)
            allowed = (
                scope == "@session"
                or (scope == "@admin" and r["system"])
                or (
                    scope in self.catalog
                    and (r["system"] or scope in json.loads(r["scopes"]))
                )
            )
            if not allowed:
                raise IdentityError(403, "Insufficient role permissions")
            return {"allowed": True, "admin": bool(r["system"])}

    def issue(self, a, s):
        now = int(time.time())
        secret = secrets.token_hex(32)
        access = str(uuid.uuid4())
        role = self.one("SELECT * FROM roles WHERE id=?", (a["role_id"],))
        self.db.execute(
            "UPDATE sessions SET refresh_hash=?,access_id=? WHERE id=?",
            (hashlib.sha256(secret.encode()).hexdigest(), access, s["id"]),
        )
        token = jwt.encode(
            {
                "sub": a["id"],
                "role_id": a["role_id"],
                "role": role["name"],
                "sid": s["id"],
                "jti": access,
                "kind": "access",
                "iss": self.issuer,
                "aud": self.audience,
                "iat": now,
                "exp": min(now + 900, s["expires_at"]),
            },
            self.secret,
            algorithm="HS256",
        )
        return {"access_token": token, "refresh_token": "hrt_" + s["id"] + "_" + secret}

    def refresh_session(self, raw):
        match = (
            re.fullmatch(r"hrt_([0-9a-f-]{36})_([0-9a-f]{64})", raw)
            if isinstance(raw, str)
            else None
        )
        if not match:
            raise IdentityError(401, "Invalid session")
        s = self.one("SELECT * FROM sessions WHERE id=?", (match[1],))
        digest = hashlib.sha256(match[2].encode()).hexdigest()
        if not s or s["revoked"] or s["expires_at"] <= time.time():
            raise IdentityError(401, "Invalid session")
        if not hmac.compare_digest(digest, s["refresh_hash"]):
            if digest in json.loads(s["used_hashes"]):
                self.db.execute("UPDATE sessions SET revoked=1 WHERE id=?", (s["id"],))
                self.db.commit()
            raise IdentityError(401, "Invalid session")
        return s

    def handle(self, method, path, body, token="", peer="local"):
        body = body or {}
        if not isinstance(body, dict):
            raise IdentityError(400, "JSON object required")
        with self.lock:
            try:
                with self.db:
                    result = self._handle(method, path, body, token, peer)
            except sqlite3.IntegrityError:
                raise IdentityError(409, "Name already exists or record is in use")
            if method != "GET":
                subject = None
                try:
                    subject = self.claims(token or result[1].get("access_token", ""))[
                        "sub"
                    ]
                except IdentityError:
                    pass
                logging.info(
                    json.dumps(
                        {
                            "event": "identity_change",
                            "method": method,
                            "path": path[:160],
                            "subject": subject,
                            "status": result[0],
                        }
                    )
                )
            return result

    def _handle(self, method, path, b, token, peer):
        if path == "/auth/login" and method == "POST":
            name = str(b.get("username", "")).lower()
            if len(name) > 128:
                name = ""
            bucket = peer + "|" + name
            now = time.time()
            self.failures = {k: v for k, v in self.failures.items() if now - v[1] < 60}
            if len(self.failures) >= 10000 and bucket not in self.failures:
                raise IdentityError(429, "Too many sign-in attempts; retry later")
            if (
                self.failures.get(bucket, (0, now))[0] >= 5
                or sum(
                    v[0] for k, v in self.failures.items() if k.startswith(peer + "|")
                )
                >= 20
            ):
                raise IdentityError(429, "Too many sign-in attempts; retry later")
            a = self.one("SELECT * FROM accounts WHERE username=?", (name,))
            valid = False
            try:
                valid = self.hasher.verify(
                    a["password_hash"] if a else self.dummy,
                    str(b.get("password", ""))[:129],
                )
            except VerificationError:
                pass
            if not a or not a["enabled"] or not valid:
                count, start = self.failures.get(bucket, (0, now))
                self.failures[bucket] = (count + 1, start)
                raise IdentityError(401, "Invalid credentials")
            self.failures.pop(bucket, None)
            self.db.execute("DELETE FROM sessions WHERE expires_at<=?", (int(now),))
            s = {"id": str(uuid.uuid4()), "expires_at": int(now) + 604800}
            self.db.execute(
                "INSERT INTO sessions VALUES(?,?,?,'[]',?,?,0)",
                (s["id"], a["id"], "", "", s["expires_at"]),
            )
            return 200, self.issue(a, s)
        if path == "/auth/forgot-password" and method == "POST":
            return self.forgot_password(b, peer)
        if path == "/auth/reset-password" and method == "POST":
            return self.reset_password(b, peer)
        if path == "/auth/refresh" and method == "POST":
            s = self.refresh_session(b.get("refresh_token"))
            a = self.one("SELECT * FROM accounts WHERE id=?", (s["account_id"],))
            if not a or not a["enabled"]:
                raise IdentityError(401, "Invalid session")
            used = json.loads(s["used_hashes"])
            if len(used) >= 4096:
                self.db.execute("UPDATE sessions SET revoked=1 WHERE id=?", (s["id"],))
                self.db.commit()
                raise IdentityError(
                    401, "Session rotation limit reached; sign in again"
                )
            used.append(s["refresh_hash"])
            self.db.execute(
                "UPDATE sessions SET used_hashes=? WHERE id=?",
                (json.dumps(used), s["id"]),
            )
            return 200, self.issue(a, s)
        if path == "/auth/logout" and method == "POST":
            try:
                s = self.refresh_session(b.get("refresh_token"))
                self.db.execute("UPDATE sessions SET revoked=1 WHERE id=?", (s["id"],))
            except IdentityError:
                pass
            return 200, {"logged_out": True}
        a, r, s = self.actor(token)
        if path == "/auth/me" and method == "GET":
            return 200, {
                "account": self.public_account(a),
                "role": self.public_role(r),
                "scopes": self.public_role(r)["scopes"],
                "admin": bool(r["system"]),
            }
        if not r["system"]:
            raise IdentityError(403, "Administrator access required")
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[1] == "accounts" and parts[3] == "reset":
            if method != "POST":
                raise IdentityError(405, "Method not allowed")
            if self.reset_mode == "off":
                raise IdentityError(404, "Not found")
            target = self.one("SELECT * FROM accounts WHERE id=?", (parts[2],))
            if not target:
                raise IdentityError(404, "Not found")
            if not target["enabled"]:
                raise IdentityError(409, "This account is disabled")
            token = self.new_reset(target["id"], 86400, a["id"])
            return 200, {
                "token": token,
                "reset_url": self.reset_link(token) if self.reset_url else None,
                "expires_in": 86400,
            }
        kind = parts[1] if len(parts) > 1 else ""
        id = parts[2] if len(parts) == 3 else None
        if kind not in ("roles", "accounts") or len(parts) not in (2, 3):
            raise IdentityError(404, "Not found")
        if method == "GET" and not id:
            return 200, [
                self.public_role(x) if kind == "roles" else self.public_account(x)
                for x in self.rows("SELECT * FROM " + kind + " ORDER BY id")
            ]
        old = self.one("SELECT * FROM " + kind + " WHERE id=?", (id,)) if id else None
        if id and not old:
            raise IdentityError(404, "Not found")
        if kind == "roles":
            if old and old["system"]:
                raise IdentityError(409, "The built-in administrator role is protected")
            if method == "DELETE" and id:
                if self.one("SELECT id FROM accounts WHERE role_id=?", (id,)):
                    raise IdentityError(409, "Role is assigned to an account")
                self.db.execute("DELETE FROM roles WHERE id=?", (id,))
                return 200, {"deleted": True}
            if method not in ("POST", "PUT") or (method == "POST") != (id is None):
                raise IdentityError(405, "Method not allowed")
            if (
                set(b) - {"name", "scopes"}
                or not isinstance(b.get("name"), str)
                or not 1 <= len(b["name"].strip()) <= 80
                or not isinstance(b.get("scopes"), list)
                or any(
                    not isinstance(x, str) or x not in self.catalog for x in b["scopes"]
                )
            ):
                raise IdentityError(
                    400, "A role name and valid scope array are required"
                )
            fields = (b["name"].strip(), json.dumps(sorted(set(b["scopes"]))))
            rid = id or str(uuid.uuid4())
            if id:
                self.db.execute(
                    "UPDATE roles SET name=?,scopes=? WHERE id=?", (*fields, id)
                )
            else:
                self.db.execute("INSERT INTO roles VALUES(?,?,?,0)", (rid, *fields))
            return (200 if id else 201), self.public_role(
                self.one("SELECT * FROM roles WHERE id=?", (rid,))
            )
        if method == "DELETE" and id:
            b = {"role_id": old["role_id"], "enabled": False}
            method = "PUT"
        if method not in ("POST", "PUT") or (method == "POST") != (id is None):
            raise IdentityError(405, "Method not allowed")
        if (
            set(b) - {"username", "password", "role_id", "enabled", "email"}
            or not isinstance(b.get("role_id"), str)
            or not self.one("SELECT id FROM roles WHERE id=?", (b["role_id"],))
            or ("enabled" in b and type(b["enabled"]) is not bool)
        ):
            raise IdentityError(
                400, "A valid role_id and boolean enabled flag are required"
            )
        enabled = b.get("enabled", bool(old["enabled"]) if old else True)
        password = b.get("password")
        if "email" in b:
            email = self.email(b["email"]) if b["email"] else None
        else:
            email = old["email"] if old else None
        if id:
            if "username" in b and b["username"] != old["username"]:
                raise IdentityError(400, "Username cannot be changed")
            if (
                old["enabled"]
                and (not enabled or old["role_id"] != b["role_id"])
                and self.one("SELECT system FROM roles WHERE id=?", (old["role_id"],))[
                    "system"
                ]
                and not self.one(
                    "SELECT a.id FROM accounts a JOIN roles r ON a.role_id=r.id WHERE a.enabled=1 AND r.system=1 AND a.id<>?",
                    (id,),
                )
            ):
                raise IdentityError(
                    409, "At least one enabled administrator is required"
                )
            password_hash = (
                self.hasher.hash(self.password(password))
                if password is not None
                else old["password_hash"]
            )
            self.db.execute(
                "UPDATE accounts SET role_id=?,enabled=?,password_hash=?,email=? WHERE id=?",
                (b["role_id"], enabled, password_hash, email, id),
            )
            if not enabled or password is not None:
                self.db.execute(
                    "UPDATE sessions SET revoked=1 WHERE account_id=?", (id,)
                )
            elif old["role_id"] != b["role_id"]:
                self.db.execute(
                    "UPDATE sessions SET access_id='' WHERE account_id=?", (id,)
                )
        else:
            id = str(uuid.uuid4())
            self.db.execute(
                "INSERT INTO accounts(id,username,password_hash,role_id,enabled,email) VALUES(?,?,?,?,?,?)",
                (
                    id,
                    self.username(b.get("username")),
                    self.hasher.hash(self.password(password)),
                    b["role_id"],
                    enabled,
                    email,
                ),
            )
        return (200 if old else 201), self.public_account(
            self.one("SELECT * FROM accounts WHERE id=?", (id,))
        )
