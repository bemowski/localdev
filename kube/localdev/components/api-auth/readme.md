# api-auth: the API host and its API keys

localdev serves browser apps on `http://localhost/<name>/`, behind SSO. APIs that tools, scripts and
other services call get their own host, **`http://api.localhost/<name>/`**:

- no SSO and no cookies: a client can't follow a Google sign-in;
- each API is either **open** or protected by **its own API keys**;
- any component adds its API with an Ingress, without touching localdev core.

Traefik does all the routing, as it does for every other route. api-auth is a small stdlib Python
service (`docker/server.py`) that answers two questions for Traefik:

1. **Is this API key valid?** It's the target of the `api/key` Middleware (Traefik `forwardAuth`),
   the same pattern as SSO, where oauth2-proxy answers. Traefik asks before passing a request to a
   keyed API; 200 lets it through, 401 rejects it. Requests never pass through api-auth.
2. **Is there any API at this path?** A catch-all route on `api.localhost` sends paths that no API
   claims to api-auth, which answers 404. Without it, `api.localhost/grafana/` would reach Grafana:
   the browser routes have no host, so they match every host.

Open APIs never touch api-auth.

## Why a service of our own

Traefik has no built-in API-key check. The alternatives were:

| option | why not |
|---|---|
| Traefik `basicAuth` | built in, but clients send `Authorization: Basic`, not an API key |
| a Traefik plugin | third-party code running inside the ingress, downloaded when Traefik starts; most keep keys in the Middleware, outside the secret store |
| Kong, APISIX, Envoy Gateway | full API gateways (consumers, rate limits, ...): a second ingress stack, far too big for this |

api-auth is about 100 lines with no dependencies. Keys live in the secret store like every other
secret: synced by `mise run secrets:sync`, overridable in `secrets.store.toml`, and listed (masked)
in cnav's Credentials tab. If we ever need rate limits, usage plans or key self-service, that's the
point to adopt a real gateway.

## Adding an API

In your component, an Ingress on host `api.localhost` and a `stripPrefix` Middleware for your path:

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: api-strip
  namespace: myapp
spec:
  stripPrefix:
    prefixes: [/myapp]
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: myapp-api
  namespace: myapp
  annotations:
    # keyed: api-key first (it reads the path before the strip). Open: leave api-key out.
    traefik.ingress.kubernetes.io/router.middlewares: api-key@kubernetescrd,myapp-api-strip@kubernetescrd
spec:
  rules:
    - host: api.localhost
      http:
        paths:
          - path: /myapp
            pathType: Prefix
            backend:
              service: {name: myapp, port: {name: http}}
```

For a keyed API, its keys go in a secret store entry named `api-<path>`, in your component's
`secrets.defaults.toml`:

```toml
[api-myapp]                      # keys for http://api.localhost/myapp/
api-key = "localdev-myapp-key"   # more keys: any name starting with api-key (api-key-2, api-key-ci, ...)
```

Then `mise run secrets:sync`. Changes apply within 5 seconds (api-auth's cache), with no restart.
For a real key, override it in your gitignored `secrets.store.toml`.

Clients send either header:

```bash
curl -H 'Authorization: Bearer localdev-whoami-key' http://api.localhost/whoami/
curl -H 'X-API-Key: localdev-whoami-key'            http://api.localhost/whoami/
```

`whoami` (`manifests/50-apps/50-1-whoami.yaml`, key `[api-whoami]` in localdev's
`secrets.defaults.toml`) is the working example. `usage` is an open API.

## Behaviour

- **The API name is the first path segment.** Keys for `/myapp` never open `/other`.
- **Fails closed.** A keyed API with no `api-<name>` entry, or one with only empty keys, rejects
  every request; api-auth logs a warning saying which entry to add.
- **Answers are JSON**: `401 {"error": "unauthorized", "message": ...}` with `WWW-Authenticate:
  Bearer`, `404 {"error": "not_found", ...}`, or `503` if it can't read the secret store.
- **Keys are compared in constant time and never logged.**
- **Access:** api-auth can `get` Secrets in namespace `secret-store` (RBAC can't narrow that to a
  name prefix). It only ever reads `api-*` entries.
- **Routing priority:** the catch-all has priority 40, its rule's length. It outranks every
  browser route (shorter rules, no host) and loses to every API Ingress (`Host(...) && PathPrefix(...)`
  is always longer).

## Names and clients

- `*.localhost` resolves to the loopback address in browsers, curl, and on Linux in general
  (systemd's nss-myhostname). On macOS, other clients may need `127.0.0.1 api.localhost` in
  `/etc/hosts`.
- Inside the cluster, call the Service directly (`http://<svc>.<namespace>.svc`). `api.localhost`
  inside a pod is the pod itself, and the Service needs no key.

## Files

| file | |
|---|---|
| `docker/server.py` | the check (`/check`), the catch-all 404, `/healthz` |
| `manifests/20-8-api-auth.yaml` | namespace `api`, Deployment, RBAC, Middleware `api/key`, the catch-all IngressRoute |
| `tests/test_server.py` | `python3 -m unittest discover -s components/api-auth/tests` |

Rebuild after editing the server: `mise run build api-auth`.
