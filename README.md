# mlflow-azure-sso

Custom [MLflow](https://mlflow.org/) tracking-server image that adds the
[`mlflow-oidc-auth`](https://github.com/mlflow-oidc/mlflow-oidc-auth) plugin so
the server can authenticate against **Azure Entra ID** (OpenID Connect) instead
of only MLflow's built-in basic auth.

The official `ghcr.io/mlflow/mlflow` images ship basic auth only. This repo
builds a drop-in replacement on top of the official `-full` image, and includes
a **simulated Entra ID** service plus an end-to-end integration test that drives
the real Authorization Code + PKCE flow.

```
Containerfile          official mlflow:v3.16.1-full + mlflow-oidc-auth
mock-azure/config.json simulated Entra ID tenant (navikt/mock-oauth2-server)
test/run-integration.sh spins up MLflow + the mock and runs the test
test/azure_sso_test.py  the assertions (login, groups, admin, denials)
.gitea/workflows/       build, push to Gitea, run the integration test
```

## Image

```
gitea.localhost/gitea_admin/mlflow:v3.16.1-entra
```

Built and pushed by the Gitea Actions workflow on every push to `main`. It is
the official `ghcr.io/mlflow/mlflow:v3.16.1-full` image plus:

| Component          | Version |
| ------------------ | ------- |
| MLflow             | 3.16.1 (pinned, `-full`) |
| mlflow-oidc-auth   | 9.0.2 |
| psycopg2 / boto3   | from the `-full` base |

## Integration test (simulated Azure Entra ID)

The test uses [`navikt/mock-oauth2-server`](https://github.com/navikt/mock-oauth2-server)
configured as an Entra-ID-shaped tenant:

- issuer `http://mock-azure:8080/11111111-2222-3333-4444-555555555555`
- the login **username** selects an Entra-shaped claim set from
  `mock-azure/config.json` (`oid`, `upn`, `preferred_username`, `given_name`,
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
| `groups` (object IDs)    | `OIDC_GROUP_NAME` / `OIDC_ADMIN_GROUP_NAME` | List the group **GUIDs** verbatim. |
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
  gitea.localhost/gitea_admin/mlflow:v3.16.1-entra \
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

### Database schema

The plugin shares MLflow's PostgreSQL database **and schema** (`mlflow`) rather
than a separate `mlflow_oidc` schema:

- point `OIDC_USERS_DB_URI` at the MLflow role/schema;
- set `OIDC_ALEMBIC_VERSION_TABLE=oidc_alembic_version` so the two Alembic
  histories do not collide on `alembic_version`.

The schema must exist before either Alembic run. The k3s deployment re-asserts
it in an `ensure-mlflow-schema` init container that runs a small Python script
(`psycopg2`) before `mlflow server`: it creates the schema if missing and, once,
drops the auth tables a previous MLflow basic-auth left behind (guarded on the
plugin's own `oidc_alembic_version` table).

## In k3s

The homelab runs this image with a **simulated Entra ID** deployed in the
`mlflow` namespace (`apps/mlflow/manifests/azure-mock.yaml`), so the SSO flow
works end-to-end without a real Azure tenant. Point
`apps/mlflow/values.yaml` → `oidcAuth.discoveryUrl` at a real tenant to switch.

## Secrets

Real Azure client secrets must not be committed. This is a throwaway homelab,
so the mock client secret is checked in; replace it with a Secret reference
(`oidcAuth.existingSecret`) before any real use.
