#!/usr/bin/env python3
"""Keycloak realm tooling for kube/localdev (driven by the keycloak:* mise tasks).

keycloak/realms/<realm>.json is the source of truth, applied by `mise run keycloak:apply`
(keycloak-config-cli). This tool goes the other way:

  export <realm>   Keycloak (admin API partial-export) -> keycloak/realms/<realm>.json

The export is trimmed to what developers manage, so files stay readable and portable across
Keycloak versions (a full export carries realm internals that keycloak-config-cli may not know):
  - clients: yours only (Keycloak's built-ins dropped); realm/client roles: yours only
  - groups; realm settings: those already in the file + a short list of commonly edited ones
  - users: kept from the existing file (partial export never includes users)
  - client secrets: restored per clientId (Keycloak masks them); internal ids dropped
Keycloak is reached at $KEYCLOAK_URL (the mise task port-forwards it) as the master-realm admin
(LOCAL_ADMIN_PASSWORD from secrets.env).
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "realms"
URL = os.environ.get("KEYCLOAK_URL", "http://localhost:18180/keycloak").rstrip("/")
MASKED = "**********"
BUILTIN_CLIENTS = {"account", "account-console", "admin-cli", "broker", "realm-management",
                   "security-admin-console"}
BUILTIN_ROLE_PREFIXES = ("default-roles-",)
BUILTIN_ROLES = {"offline_access", "uma_authorization"}
# realm-level settings worth round-tripping even if the file doesn't have them yet
REALM_SETTINGS = {
    "realm", "enabled", "displayName", "displayNameHtml", "loginWithEmailAllowed", "registrationAllowed",
    "registrationEmailAsUsername", "resetPasswordAllowed", "rememberMe", "verifyEmail", "editUsernameAllowed",
    "duplicateEmailsAllowed", "sslRequired", "loginTheme", "accountTheme", "emailTheme",
    "internationalizationEnabled", "supportedLocales", "defaultLocale", "passwordPolicy", "bruteForceProtected",
    "accessTokenLifespan", "ssoSessionIdleTimeout", "ssoSessionMaxLifespan", "offlineSessionIdleTimeout",
}


def die(msg):
    sys.exit(f"realmtool: {msg}")


def call(method, path, data=None, token=None, form=False):
    body, headers = None, {}
    if form:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(URL + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        die(f"{method} {path}: HTTP {e.code} {e.read()[:200]!r}")


def admin_token():
    password = os.environ.get("LOCAL_ADMIN_PASSWORD") or die("LOCAL_ADMIN_PASSWORD not set")
    return call("POST", "/realms/master/protocol/openid-connect/token", form=True, data={
        "grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": password,
    })["access_token"]


def strip_ids(o):
    if isinstance(o, dict):
        return {k: strip_ids(v) for k, v in o.items() if k not in ("id", "containerId")}
    if isinstance(o, list):
        return [strip_ids(v) for v in o]
    return o


def cmd_export(args):
    realm = args[0] if args else "localdev"
    out = ROOT / f"{realm}.json"
    old = json.loads(out.read_text()) if out.exists() else {}
    exported = call("POST", f"/admin/realms/{realm}/partial-export?exportClients=true&exportGroupsAndRoles=true",
                    token=admin_token())
    full = strip_ids(exported)
    keep = REALM_SETTINGS | {k for k in old if not isinstance(old[k], (dict, list))}
    new = {k: v for k, v in full.items() if k in keep and not isinstance(v, (dict, list))}
    new["realm"] = realm
    builtin_role = lambda r: r["name"] in BUILTIN_ROLES or r["name"].startswith(BUILTIN_ROLE_PREFIXES)
    clients = [c for c in full.get("clients", []) if c["clientId"] not in BUILTIN_CLIENTS]
    names = {c["clientId"] for c in clients}
    new["roles"] = {"realm": [r for r in full.get("roles", {}).get("realm", []) if not builtin_role(r)]}
    client_roles = {k: v for k, v in full.get("roles", {}).get("client", {}).items() if k in names and v}
    if client_roles:
        new["roles"]["client"] = client_roles
    new["groups"] = full.get("groups", [])
    new["clients"] = clients

    old_secrets = {c["clientId"]: c.get("secret") for c in old.get("clients", []) if c.get("secret")}
    for c in new.get("clients", []):
        if c.get("secret") == MASKED:
            if old_secrets.get(c["clientId"]):
                c["secret"] = old_secrets[c["clientId"]]
            else:
                del c["secret"]   # keycloak keeps its generated one; set it in the file to pin it
    if "users" in old:
        new["users"] = old["users"]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
    print(f"  wrote {out.relative_to(ROOT.parent.parent)}: {len(new.get('clients', []))} clients, "
          f"{len(new.get('roles', {}).get('realm', []))} realm roles, {len(new.get('groups', []))} groups, "
          f"{len(new.get('users', []))} users (kept from file)")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "export":
        sys.exit(__doc__)
    cmd_export(sys.argv[2:])
