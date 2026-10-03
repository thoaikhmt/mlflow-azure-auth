# Custom MLflow tracking server image with the `mlflow-oidc-auth` plugin, so the
# server can authenticate against Azure Entra ID (OIDC) instead of only MLflow's
# built-in basic auth.
#
# Base is the official `-full` image: it already bundles the Postgres/S3 drivers
# (`psycopg2`, `boto3`) and pins MLflow to the same version we want. We only add
# the plugin and pin MLflow so pip cannot drift the MLflow version the plugin
# was validated against (`mlflow-oidc-auth` allows `mlflow>=3.16,<4`).
#
# Only the base package is installed on purpose: the `[saml]`/`[full]` extras
# build `xmlsec` from source and the `-full` image lacks the dev headers, and
# Entra ID SSO is plain OIDC.
#
# The plugin comes from the fork thoaikhmt/mlflow-oidc-auth, pinned to the commit
# that fixes --static-prefix handling (`_prefix.py` derived the API path set from
# the static prefix, so prefixed UI paths were denied with 401 JSON instead of
# redirecting to login). Bump MLFLOW_OIDC_AUTH_REF when the fix moves.
FROM ghcr.io/mlflow/mlflow:v3.16.1-full

ARG MLFLOW_VERSION=3.16.1
ARG MLFLOW_OIDC_AUTH_REF=b8f0f67506127233b68d6dc8245ef7c1e1134fee

USER root

RUN pip install --no-cache-dir \
        "mlflow==${MLFLOW_VERSION}" \
        "mlflow-oidc-auth @ https://github.com/thoaikhmt/mlflow-oidc-auth/archive/${MLFLOW_OIDC_AUTH_REF}.tar.gz"

# Fail the build if the plugin (or a driver it needs) is not importable. The
# Entra ID group plugin resolves group object IDs to names via Microsoft Graph.
RUN python - <<'PY'
import importlib.metadata as md

import mlflow
import mlflow_oidc_auth  # noqa: F401  (registers the `oidc-auth` MLflow app)
from mlflow_oidc_auth.plugins import group_detection_microsoft_entra_id  # noqa: F401

print("mlflow                 ", mlflow.__version__)
print("mlflow-oidc-auth       ", md.version("mlflow-oidc-auth"))
print("psycopg2-binary        ", md.version("psycopg2-binary"))
print("boto3                  ", md.version("boto3"))
PY
