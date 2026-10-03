#!/usr/bin/env bash
# End-to-end integration test: the custom MLflow image + mlflow-oidc-auth
# against a simulated Azure Entra ID (navikt/mock-oauth2-server).
#
# The suite runs twice: once bare and once with `--static-prefix=/mlflow`
# (the way the k3s deployment serves MLflow), because the plugin keeps its
# fixed routes at the server root while MLflow's UI/API move under the prefix.
#
#   ./test/run-integration.sh              # podman or docker
#   IMAGE=my/mlflow:test ./test/run-integration.sh
#
# Everything runs on a private container network, so the service names
# (`mlflow`, `mock-azure`) resolve for both the server and the test client and
# the redirect URIs line up. No host ports are published.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENGINE="${CONTAINER_ENGINE:-}"
if [ -z "$ENGINE" ]; then
  if command -v docker >/dev/null 2>&1; then ENGINE=docker
  elif command -v podman >/dev/null 2>&1; then ENGINE=podman
  else echo "need docker or podman" >&2; exit 1
  fi
fi

IMAGE="${IMAGE:-mlflow-azure-sso:test}"
MOCK_IMAGE="${MOCK_IMAGE:-ghcr.io/navikt/mock-oauth2-server:6.0.4}"
NET="${NET:-mlflow-azure-sso-test}"
TENANT="${TENANT:-11111111-2222-3333-4444-555555555555}"

# SELinux (Fedora/RHEL) blocks the container from reading the bind-mounted mock
# config unless the mount is relabelled; Docker has no `:Z` modifier.
MOUNT_OPTS="ro"
if [ "$ENGINE" = "podman" ]; then MOUNT_OPTS="ro,Z"; fi

cleanup() {
  "$ENGINE" rm -f mlflow mock-azure >/dev/null 2>&1 || true
  "$ENGINE" network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [ "${FORCE_BUILD:-0}" != "1" ] && "$ENGINE" image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "==> using existing $IMAGE (FORCE_BUILD=1 to rebuild)"
else
  echo "==> building $IMAGE"
  "$ENGINE" build -t "$IMAGE" -f Containerfile .
fi

echo "==> creating network $NET"
"$ENGINE" network rm "$NET" >/dev/null 2>&1 || true
"$ENGINE" network create "$NET" >/dev/null

echo "==> starting simulated Azure Entra ID (mock-oauth2-server)"
"$ENGINE" run -d --name mock-azure --network "$NET" \
  -v "$ROOT_DIR/mock-azure/config.json:/config.json:$MOUNT_OPTS" \
  -e JSON_CONFIG_PATH=/config.json \
  "$MOCK_IMAGE" >/dev/null

# Start MLflow (optionally with a static prefix), wait until healthy, then run
# the test client on the same network.
run_suite() {
  local prefix="$1"
  local label="${prefix:-<none>}"
  local static_args=()
  if [ -n "$prefix" ]; then static_args=(--static-prefix="$prefix"); fi

  echo
  echo "==> MLflow suite: --static-prefix='${label}'"
  "$ENGINE" rm -f mlflow >/dev/null 2>&1 || true
  "$ENGINE" run -d --name mlflow --network "$NET" \
    -e OIDC_DISCOVERY_URL="http://mock-azure:8080/${TENANT}/.well-known/openid-configuration" \
    -e OIDC_CLIENT_ID=mlflow-tracking \
    -e OIDC_CLIENT_SECRET=mlflow-secret \
    -e OIDC_REDIRECT_URI="http://mlflow:5000/callback" \
    -e OIDC_SCOPE="openid,email,profile" \
    -e OIDC_USERNAME_FIELD="oid" \
    -e OIDC_DISPLAY_NAME_FIELD="name" \
    -e OIDC_GROUP_NAME="22222222-2222-2222-2222-222222222222" \
    -e OIDC_ADMIN_GROUP_NAME="33333333-3333-3333-3333-333333333333" \
    -e OIDC_PROVIDER_DISPLAY_NAME="Sign in with Azure Entra ID" \
    -e OIDC_GROUPS_ATTRIBUTE=groups \
    -e OIDC_ALEMBIC_VERSION_TABLE=oidc_alembic_version \
    -e DEFAULT_MLFLOW_PERMISSION=MANAGE \
    -e AUTOMATIC_LOGIN_REDIRECT=true \
    -e SESSION_COOKIE_SECURE=false \
    -e SESSION_COOKIE_SAMESITE=lax \
    -e OIDC_USERS_DB_URI="sqlite:////tmp/oidc-auth.db" \
    -e SECRET_KEY="integration-test-secret-key" \
    "$IMAGE" \
    mlflow server --app-name oidc-auth --host 0.0.0.0 --port 5000 \
      "${static_args[@]}" \
      --allowed-hosts='mlflow:*,localhost:*,127.0.0.1:*' \
      --backend-store-uri sqlite:////tmp/mlflow.db \
      --default-artifact-root /tmp/mlruns --workers 1 >/dev/null

  echo "==> waiting for MLflow to become ready"
  local ready=false
  for _ in $(seq 1 60); do
    if "$ENGINE" exec mlflow python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=3)" \
        >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 3
  done
  if [ "$ready" != true ]; then
    echo "MLflow did not become ready; logs:" >&2
    "$ENGINE" logs mlflow >&2 || true
    exit 1
  fi

  echo "==> running integration test (prefix='${label}')"
  "$ENGINE" run --rm --network "$NET" \
    -v "$ROOT_DIR/test:/test:$MOUNT_OPTS" \
    -e MLFLOW_BASE="http://mlflow:5000" \
    -e MLFLOW_STATIC_PREFIX="$prefix" \
    -e OIDC_PROVIDER=default \
    -e OIDC_GROUP_NAME="22222222-2222-2222-2222-222222222222" \
    -e OIDC_ADMIN_GROUP_NAME="33333333-3333-3333-3333-333333333333" \
    -e EXPECTED_USERNAME="44444444-4444-4444-4444-444444444444" \
    -e OIDC_PROVIDER_DISPLAY_NAME="Sign in with Azure Entra ID" \
    "$IMAGE" python /test/azure_sso_test.py
}

run_suite ""
run_suite "/mlflow"
