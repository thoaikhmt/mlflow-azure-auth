# MLflow Azure Auth

Custom [MLflow](https://mlflow.org/) tracking-server image that adds the
`mlflow-oidc-auth` plugin so the server can authenticate against **Azure Entra
ID** (OpenID Connect) instead of only MLflow's built-in basic auth.

The official `ghcr.io/mlflow/mlflow` images ship basic auth only. This repo
builds a drop-in replacement on top of the official `-full` image, and includes
a **simulated Entra ID** service plus an end-to-end integration test that drives
the real Authorization Code + PKCE flow.

```
Containerfile              official mlflow:v3.16.1-full + vendored mlflow-oidc-auth
mlflow-oidc-auth/          vendored plugin source (see "Vendored plugin" below)
test/mock-azure/config.json  simulated Entra ID tenant
test/run-integration.sh    spins up MLflow + the mock and runs the test
test/azure_sso_test.py     the assertions (login, groups, admin, denials, UI)
.github/workflows/         build and push the image to Docker Hub (GitHub Actions)
```

## Image

```
docker.io/thoaikhmt/mlflow-azure-auth:v1.2.0
```

Built and pushed by the `.github/workflows/build-push.yaml` workflow: every push
to `main` publishes `:latest`, and every `v*` git tag publishes an image with the
same name as the tag (the current release is `v1.2.0`). It is the official
`ghcr.io/mlflow/mlflow:v3.16.1-full` image plus:

| Component          | Version |
| ------------------ | ------- |
| MLflow             | 3.16.1 (pinned, `-full`) |
| mlflow-oidc-auth   | vendored (`mlflow-oidc-auth/`) |
| psycopg2 / boto3   | from the `-full` base |

### Vendored plugin

The plugin source is **vendored** under `mlflow-oidc-auth/` instead of being
pip-installed from an upstream source archive. Installing from the archive
silently shipped **no admin UI**:
`mlflow_oidc_auth/ui` is the React build output, is git-ignored, and is not in
the archive, so the wheel had no `ui/` directory and every `/oidc/ui/*` request
died with `RuntimeError: UI directory not found` (HTTP 500) — the "Permissions"
page was unreachable. The `Containerfile` now builds the frontend in a `node:24`
stage (Vite writes to `mlflow_oidc_auth/ui`) and installs the plugin from the
vendored source with those assets in place; a build-time assertion fails the
build if `ui/index.html` is missing from the installed package.

Patches are carried on top of the vendored source:

- `mlflow_oidc_auth/routers/_prefix.py` — under `--static-prefix`,
  `_get_rest_path()` already includes the prefix, so the old code derived the
  API-path set from the prefix (e.g. `/mlflow`) and treated `/mlflow/...` as an
  API path: unauthenticated browsers got `401 {"detail":"Authentication
  required"}` instead of being redirected to login. The fix strips the prefix
  before deriving the segment and prepends it to the resulting `/api` ·
  `/ajax-api` prefixes.
- `mlflow_oidc_auth/hack/menu.html` and `hack/reauth.html` — the injected menu
  and re-auth helper hardcoded root-absolute hrefs (`/oidc/ui/user`,
  `/logout`, `/login`). Those are right only when the plugin serves at the
  domain root. When MLflow is mounted under a prefix with the ASGI `root_path`
  (`uvicorn --root-path /mlflow`, behind a proxy that strips `/mlflow`), the
  plugin's own routes move under that prefix too, so the browser hit
  `/oidc/ui/user` (404). The snippets now carry a `__OIDC_BASE_PATH__` token
  that `hack.py` substitutes, per request, with the deployment base path
  (`request.script_root`): `/mlflow` behind such a proxy, empty otherwise
  (e.g. under MLflow's `--static-prefix`, which keeps the plugin at the root).
- `mlflow_oidc_auth/hack/default-model.html` (new) — MLflow's judge, guardrail
  and issue-detection dialogs auto-select the **first** endpoint returned by
  `GET ajax-api/3.0/mlflow/gateway/endpoints/list`, so a configured default was
  ignored. The snippet is injected only when `MLFLOW_GENAI_JUDGE_DEFAULT_MODEL`
  is set (via the plugin's config chain), patches `fetch`, and moves the
  endpoint the value names to the front before the UI reads the list. Accepted
  values: `gateway:/my-endpoint`, a bare `my-endpoint`, or
  `<provider>:/<model>` (matched against the endpoint's model definition).
  Disable with `EXTEND_MLFLOW_DEFAULT_MODEL=false`.

Bump the vendored copy by re-syncing `mlflow-oidc-auth/` and re-applying the
patches above.

## Integration test (simulated Azure Entra ID)

The test uses `mock-oauth2-server` configured as an Entra-ID-shaped tenant:

- issuer `http://mock-azure:8080/11111111-2222-3333-4444-555555555555`
- the login **username** selects an Entra-shaped claim set from
  `test/mock-azure/config.json` (`oid`, `upn`, `preferred_username`, `given_name`,
  `family_name`, `name`, `groups`, `tid`). Like real Entra ID, `groups` carries
  group **object IDs** (GUIDs), and the account key is the `oid` claim.

| Username | `oid` (username)                       | `groups` (object IDs)                    | Result              |
| -------- | -------------------------------------- | ---------------------------------------- | ------------------- |
| `alice`  | `44444444-…`                           | `22222222-…`                              | logs in (non-admin) |
| `admin`  | `55555555-…`                           | `22222222-…`, `33333333-…`                | logs in as admin    |
| `carol`  | `77777777-…`                           | `88888888-…`, `22222222-…`                | logs in (non-admin) |
| `bob`    | `66666666-…`                           | *(none)*                                  | refused by group gate |

A user is admitted when **any** group in their `groups` list matches **any**
entry in `OIDC_GROUP_NAME` (`carol` matches on `22222222-…` even though she also
belongs to `88888888-…`); all her Entra groups are synced to MLflow.

### Entra ID claim mapping

The plugin persists only two user columns, `username` and `display_name` (there
is no separate email/first/last column), so configure:

| Entra claim              | Plugin setting                        | Notes |
| ------------------------ | ------------------------------------- | ----- |
| `oid`                    | `OIDC_USERNAME_FIELD=oid`             | Stable object id; survives UPN changes. |
| `name`                   | `OIDC_DISPLAY_NAME_FIELD=name`        | Entra builds this from `given_name`/`family_name`; the plugin cannot store first/last separately. |
| `groups` (object IDs)    | `OIDC_GROUP_NAME` / `OIDC_ADMIN_GROUP_NAME` | List the group **GUIDs** verbatim, comma- or newline-separated. |
| `email` / `upn` / `preferred_username` | *(not used)*          | Only relevant if you key accounts by email (`OIDC_USERNAME_FIELD=email,preferred_username,upn`). |

For human-readable group names instead of GUIDs, set
`OIDC_GROUP_DETECTION_PLUGIN=mlflow_oidc_auth.plugins.group_detection_microsoft_entra_id`
(the app then needs admin-consented Graph `GroupMember.Read.All`).

Run it (needs `docker` or `podman`):

```bash
./test/run-integration.sh
# podman on Fedora: the script adds :Z for SELinux automatically
# force a rebuild:  FORCE_BUILD=1 ./test/run-integration.sh
```

The script builds the image, starts the mock and MLflow on a private network,
and asserts: the provider is advertised, login uses PKCE, the user is
provisioned with the right groups, the admin group grants admin, a user in no
allowed group is refused, and unauthenticated API calls are rejected.

## Using the image

```bash
docker run --rm -p 5000:5000 \
  -e OIDC_DISCOVERY_URL="https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration" \
  -e OIDC_CLIENT_ID="<application (client) id>" \
  -e OIDC_CLIENT_SECRET="<client secret>" \
  -e OIDC_REDIRECT_URI="https://mlflow.example.com/callback" \
  -e OIDC_SCOPE="openid,email,profile" \
  -e OIDC_USERNAME_FIELD="oid" \
  -e OIDC_DISPLAY_NAME_FIELD="name" \
  -e OIDC_GROUPS_ATTRIBUTE="groups" \
  -e OIDC_GROUP_NAME="<allowed group object id>" \
  -e OIDC_ADMIN_GROUP_NAME="<admin group object id>" \
  -e OIDC_USERS_DB_URI="postgresql+psycopg2://mlflow:pass@db:5432/mlflow" \
  -e OIDC_ALEMBIC_VERSION_TABLE="oidc_alembic_version" \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  thoaikhmt/mlflow-azure-auth:v1.2.0 \
  mlflow server --app-name oidc-auth --host 0.0.0.0 --port 5000 \
    --backend-store-uri postgresql:// --default-artifact-root s3://mlflow/
```

### Entra ID specifics

- **Use the tenant-specific v2.0 discovery URL** (`.../<tenant-id>/v2.0/...`),
  not `common`/`organizations`.
- Register the redirect URI under the **Web** platform of the app registration.
- `groups` arrive as **object IDs**. Either list those IDs in
  `OIDC_GROUP_NAME`/`OIDC_ADMIN_GROUP_NAME`, or resolve them to names with the
  bundled Microsoft Graph plugin:

  ```bash
  OIDC_GROUP_DETECTION_PLUGIN=mlflow_oidc_auth.plugins.group_detection_microsoft_entra_id
  OIDC_SCOPE="openid,email,profile,https://graph.microsoft.com/GroupMember.Read.All"
  ```

  (the app needs admin-consented delegated `GroupMember.Read.All`).
- If a user is in more than 200 groups Entra omits the `groups` claim; the Graph
  plugin is required for those users.
- Behind a reverse proxy set `TRUSTED_PROXIES` or an explicit
  `OIDC_REDIRECT_URI`, otherwise the callback URL is built from the internal
  address. For self-signed homelab certs set `OIDC_VERIFY_SSL=false`.

### Non-interactive clients (pods, CI, training jobs)

A pod in EKS (or any non-browser client) does **not** run the Entra SSO flow.
It authenticates with a **named access token** presented as the password of HTTP
basic auth (`username:token`). All three steps below are done in the MLflow UI
(served at `/oidc/ui`, the "Permissions" item in the MLflow menu) as an
administrator.

**1. Create the identity and issue a token.**

- Open **Users** (a person) or **Service Accounts** (an M2M identity — create one
  with **Create Service Account**), then open the identity.
- Open its **Tokens** tab and create a token (name it, set an expiry). The
  plaintext `mlf_<prefix>_<secret>` is shown **once** — copy it now.

**2. Grant permissions.**

A new identity has no grants, and the default is deny-by-default, so tracking and
prompt calls return 403 until it is granted. Grant a **regex pattern** once so
every current and future experiment/prompt is covered, instead of per resource:

- Open the identity's **Experiments** tab, toggle **Regex Mode** on, click
  **Add New Regex Rule**, set Regex `.*`, Priority `0`, Permissions `EDIT` (use
  `MANAGE` if the client must delete experiments) and save.
- Open the **Prompts** tab (Regex Mode stays on) and add the same rule there.
- Add rules on **Models** / **AI Endpoints** etc. if the client uses them.

**3. Configure the pod.**

```bash
MLFLOW_TRACKING_URI=https://mlflow.example.com
MLFLOW_TRACKING_USERNAME=training-bot        # plugin username, or a service-account name
MLFLOW_TRACKING_PASSWORD=mlf_<prefix>_<secret>
MLFLOW_TRACKING_INSECURE_TLS=true            # optional: accept a self-signed cert
```

`MLFLOW_TRACKING_USERNAME` is the name the plugin stored (the Entra `oid` GUID
when `OIDC_USERNAME_FIELD=oid`, or the service-account name). Keep the token in a
Kubernetes Secret. `MLFLOW_TRACKING_INSECURE_TLS` is a **client-side** variable
read by the pod's MLflow client — setting it on the server has no effect. `true`
makes the client skip certificate verification, so it connects whether the
server's certificate is self-signed or CA-issued; prefer mounting the CA over
disabling verification where you can.

No Entra round trip is involved: the token is verified against the plugin's
`user_tokens` table, then the normal per-resource permission checks apply.

Because a polling pod would otherwise pay a database lookup plus a hash check on
every request, successful token verifications are cached in-process:

| Env var | Default | Meaning |
| ------- | ------- | ------- |
| `OIDC_BASIC_AUTH_CACHE_TTL_SECONDS` | `30` | How long a successful token check is remembered. `0` disables the cache. |
| `OIDC_BASIC_AUTH_CACHE_MAX_SIZE` | `4096` | Distinct credentials kept per process. |
| `OIDC_BASIC_AUTH_WORKERS` | `1` | Concurrent first-time verifications per process. Raise for a large burst of cold clients. |

Notes:

- The cache is **per process** and keyed by a digest of `username:token`; the
  plaintext token is never retained.
- A token that is deleted or expires keeps working for at most the cache TTL.
  Deactivating the account takes effect immediately (the per-request profile
  lookup is unaffected).
- The cache is a performance optimization, not a security boundary. Pods still
  authenticate; they just skip Entra.

### Internal job authentication (UI-triggered evaluation, scoring, issues)

MLflow runs UI-triggered jobs — **Run judges / evaluation**, online scoring,
issue detection, prompt optimization — in a process it spawns, which then calls
the tracking server back with the MLflow client. Those callbacks carry no user
session, so without help they fail with `Authentication required`.

MLflow's job functions already know how to fix this: when it is available they
present `_MLFLOW_INTERNAL_GATEWAY_AUTH_TOKEN` as the basic-auth password and the
**name of the user who triggered the job** as the username
(`mlflow/genai/evaluation/job.py`). MLflow only generates that token for its own
`basic-auth` app, so this plugin supplies it:

- on startup the plugin derives a token from `SECRET_KEY` and exports it, so
  every job subprocess inherits it;
- a request that presents it is authenticated as the named user **without** a
  token-store lookup, and then goes through the normal per-resource permission
  checks — the job can only do what that user could.

The token never leaves the server: it is not returned to any client and not
logged, and only processes the server itself started hold it. Keep `SECRET_KEY`
configured (it is required for stable sessions anyway); with a per-process random
key no token is derivable and the feature stays off.

| Env var | Default | Meaning |
| ------- | ------- | ------- |
| `OIDC_INTERNAL_AUTH_ENABLED` | `true` | Trust job subprocesses. Set `false` to require credentials from every caller. |
| `OIDC_INTERNAL_AUTH_TOKEN` | *(derived)* | Pin the token instead of deriving it from `SECRET_KEY` (for example to share it with an external job runner). Treat it as a secret. |

### Faster UI requests (session-resolution cache)

Once a user has signed in, every request from the MLflow UI is authenticated with
the browser session. That ran one database statement per request
(`resolve_auth_session`) to confirm the session row is live and to read the
user's flags — negligible for a single call, but the **playground** and
**evaluation** screens issue bursts of requests, so the pages felt slow even
though per-resource permission decisions were already cached.

The plugin now remembers the resolved session for a short window
(`OIDC_SESSION_CACHE_TTL_SECONDS`, default 30s — the same lifetime as the
permission caches), so those bursts skip the statement. The lookup also runs off
the event loop, so a cache miss no longer stalls every other request the worker
is handling.

| Env var | Default | Meaning |
| ------- | ------- | ------- |
| `OIDC_SESSION_CACHE_TTL_SECONDS` | `30` | How long a resolved session is remembered. `0` disables the cache and re-resolves every request. |
| `OIDC_SESSION_CACHE_MAX_SIZE` | `4096` | Distinct sessions kept per process (or in Redis). |

Notes:

- Logout, a single-session revoke, and a silent token refresh invalidate exactly
  the affected entry, so they take effect immediately.
- Bulk revocation — deactivating a user, deleting one, an administrator revoking
  all sessions — takes effect within the TTL. Set the TTL to `0` if your policy
  requires deactivation to deny on the very next request.
- With the default local backend the cache is per process; use
  `CACHE_BACKEND=redis` to share it (and the permission caches) across replicas.


### Database schema

The plugin shares MLflow's PostgreSQL database **and schema** (`mlflow`) rather
than a separate `mlflow_oidc` schema:

- point `OIDC_USERS_DB_URI` at the MLflow role/schema;
- set `OIDC_ALEMBIC_VERSION_TABLE=oidc_alembic_version` so the two Alembic
  histories do not collide on `alembic_version`.

The schema must exist before either Alembic run. The k3s deployment re-asserts
it in a `dbchecker` init container (which replaces the chart's built-in one) that
runs a small Python script (`psycopg2`) before `mlflow server`: it waits for
PostgreSQL, creates the schema if missing and, once, drops the auth tables a
previous MLflow basic-auth left behind (guarded on the plugin's own
`oidc_alembic_version` table).

## Default AI Gateway endpoint

At startup the plugin can seed a **single default AI Gateway endpoint** from
environment variables, so MLflow's judge / guardrail / issue-detection dialogs
have an endpoint to work with on a fresh deployment. It mirrors the built-in
"default workspace" seeder: it runs on every start, creates only the records that
are missing, logs a warning instead of failing startup, and never raises.

It writes three records through MLflow's native gateway store: a **secret**, a
**model definition**, and an **endpoint** (PRIMARY linkage).

### Recommended: reference an existing secret

Create the secret once (MLflow UI → **AI Gateway → Secrets**, or an admin job) —
provider, auth mode, `api_base` and credentials live there, encrypted at rest.
Then the configuration carries **no credential material**:

| Env var | Meaning |
| ------- | ------- |
| `MLFLOW_GATEWAY_DEFAULT_PROVIDER` | Gateway provider (`openai`, `anthropic`, `gemini`, `bedrock`, `databricks`, ...) |
| `MLFLOW_GATEWAY_DEFAULT_MODEL` | Provider model id / deployment / serving endpoint name |
| `MLFLOW_GATEWAY_DEFAULT_SECRET_NAME` | Name of the existing gateway secret to reference |
| `MLFLOW_GATEWAY_DEFAULT_ENDPOINT` | Gateway endpoint name (optional) |

```bash
MLFLOW_GATEWAY_DEFAULT_PROVIDER=anthropic
MLFLOW_GATEWAY_DEFAULT_MODEL=claude-sonnet-4-5
MLFLOW_GATEWAY_DEFAULT_SECRET_NAME=corp-anthropic-key
MLFLOW_GATEWAY_DEFAULT_ENDPOINT=corp-judge
MLFLOW_GENAI_JUDGE_DEFAULT_MODEL=gateway:/corp-judge
```

If the named secret does not exist, seeding is skipped and startup logs a warning
(no partial records).

### Inline secret (dev / one-shot)

Instead of naming a secret, create it from individual env vars — one per field,
plain scalars (no JSON), so each can come from its own Kubernetes Secret key:

- `MLFLOW_GATEWAY_DEFAULT_SECRET_<FIELD>` → secret value (e.g. `..._SECRET_API_KEY`)
- `MLFLOW_GATEWAY_DEFAULT_AUTH_CONFIG_<FIELD>` → `auth_config` (e.g. `..._AUTH_CONFIG_API_BASE`)

`<FIELD>` is case-insensitive (`API_KEY` → `api_key`). The endpoint name is
`MLFLOW_GATEWAY_DEFAULT_ENDPOINT`, otherwise derived from
`MLFLOW_GENAI_JUDGE_DEFAULT_MODEL` (`gateway:/corp-judge` → `corp-judge`).

### Databricks shorthand

`DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET` and
`DATABRICKS_MODEL` are a shorthand for `provider=databricks` with OAuth M2M
credentials; add `MLFLOW_GATEWAY_DEFAULT_SECRET_NAME` to reuse an existing
secret instead of creating one. `MLFLOW_GATEWAY_DEFAULT_*` takes precedence over
the shorthand.

```bash
DATABRICKS_HOST=https://adb-....azuredatabricks.net
DATABRICKS_CLIENT_ID=<service-principal-id>
DATABRICKS_CLIENT_SECRET=<service-principal-secret>
DATABRICKS_MODEL=ds1-dev-apse-1-ml-endpoints-bedrock-claude-sonnet-4-5
MLFLOW_GENAI_JUDGE_DEFAULT_MODEL=gateway:/corp-judge
```

With `MLFLOW_ENABLE_WORKSPACES=true` the endpoint and secret are created in the
`default` workspace, so the secret must exist there.

See `.env.example` for the full list.

## In k3s

The homelab runs this image with a **simulated Entra ID** deployed in the
`mlflow` namespace (`apps/mlflow/manifests/azure-mock.yaml`), so the SSO flow
works end-to-end without a real Azure tenant. Point
`apps/mlflow/values.yaml` → `oidcAuth.discoveryUrl` at a real tenant to switch.

## Secrets

Real Azure client secrets must not be committed. This is a throwaway homelab,
so the mock client secret is checked in; replace it with a Secret reference
(`oidcAuth.existingSecret`) before any real use.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
