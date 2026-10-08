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
# The plugin source is VENDORED under ./mlflow-oidc-auth so we own and can patch
# it (including the --static-prefix handling in `_prefix.py`). Installing
# straight from an upstream source tarball silently shipped NO admin UI:
# `mlflow_oidc_auth/ui` is the React build output, is git-ignored, and is not in
# the archive, so `pip install` produced a wheel without it and every
# `/oidc/ui/*` request died with `RuntimeError: UI directory not found`. The
# `ui` stage below builds the frontend first; the runtime stage installs the
# plugin from source with the built assets in place. We also fixed
# `hack/menu.html`, whose relative `oidc/ui/user` / `logout` links resolved
# under /mlflow and 404'd.

# ── Stage 1: build the React admin UI into mlflow_oidc_auth/ui ──────────────
# Vite is configured with `outDir: ../mlflow_oidc_auth/ui`, so building from
# web-react/ writes the bundle into the sibling package directory.
FROM node:24-bookworm-slim AS ui

WORKDIR /plugin

# The node:24 image already ships yarn 1.22.22, which honours the v1 yarn.lock.
RUN yarn --version

COPY mlflow-oidc-auth/web-react/package.json mlflow-oidc-auth/web-react/yarn.lock ./web-react/
RUN yarn --cwd web-react install --frozen-lockfile

COPY mlflow-oidc-auth/web-react/ ./web-react/
RUN yarn --cwd web-react build \
    && test -f mlflow_oidc_auth/ui/index.html

# ── Stage 2: runtime image (official -full + vendored plugin + built UI) ────
FROM ghcr.io/mlflow/mlflow:v3.17.0-full

ARG MLFLOW_VERSION=3.17.0
ARG MLFLOW_OIDC_AUTH_VERSION=7.0.0

USER root

# Stamp the plugin's dynamic version (setuptools reads this at build time)
# instead of the source default `7.0.0.dev0`.
ENV MLFLOW_OIDC_AUTH_VERSION=${MLFLOW_OIDC_AUTH_VERSION}

# Copy the vendored plugin source, then overwrite/ensure the built UI is present.
COPY mlflow-oidc-auth/ /opt/mlflow-oidc-auth/
COPY --from=ui /plugin/mlflow_oidc_auth/ui /opt/mlflow-oidc-auth/mlflow_oidc_auth/ui

RUN pip install --no-cache-dir \
        "mlflow==${MLFLOW_VERSION}" \
        /opt/mlflow-oidc-auth

# Fail the build if the plugin (or a driver it needs) is not importable, or if
# the admin UI did not make it into the installed package. The Entra ID group
# plugin resolves group object IDs to names via Microsoft Graph.
RUN python - <<'PY'
import importlib.metadata as md
import os

import mlflow
import mlflow_oidc_auth  # noqa: F401  (registers the `oidc-auth` MLflow app)
from mlflow_oidc_auth.plugins import group_detection_microsoft_entra_id  # noqa: F401

ui_dir = os.path.join(os.path.dirname(mlflow_oidc_auth.__file__), "ui")
index = os.path.join(ui_dir, "index.html")
assert os.path.isfile(index), f"admin UI missing from installed package: {index}"

print("mlflow                 ", mlflow.__version__)
print("mlflow-oidc-auth       ", md.version("mlflow-oidc-auth"))
print("admin UI               ", ui_dir)
print("psycopg2-binary        ", md.version("psycopg2-binary"))
print("boto3                  ", md.version("boto3"))
PY
