"""
Router prefix constants for the FastAPI application.

This module defines all router prefixes used throughout the application
to ensure consistency and easy maintenance of URL structures.
"""

import os
from typing import Optional

from mlflow.server.handlers import (
    STATIC_PREFIX_ENV_VAR,
    _get_ajax_path,
    _get_rest_path,
)

# MLflow serves every REST handler under two prefixes: "/api/..." for API clients
# and "/ajax-api/..." for its own web UI. Derive both leading segments from
# MLflow instead of hardcoding them, so an upstream rename cannot silently
# desynchronise this plugin's routes from the paths the UI calls.
#
# When MLflow runs with `--static-prefix`, `_get_rest_path` already includes it
# (e.g. "/mlflow/api/2.0/probe"). Taking the first path segment then yields the
# static prefix ("mlflow") instead of "api", which made every prefixed UI path
# look like an API path: unauthenticated browsers got a 401 JSON instead of a
# redirect to login. Strip the static prefix before taking the segment, and put
# it back in front of the derived prefixes.
_STATIC_PREFIX = (os.environ.get(STATIC_PREFIX_ENV_VAR) or "").rstrip("/")


def _segment(full_path: str) -> str:
    """Return the REST / ajax-api segment of ``full_path``, ignoring any prefix."""
    path = full_path
    if _STATIC_PREFIX and path.startswith(_STATIC_PREFIX):
        path = path[len(_STATIC_PREFIX) :]
    return path.lstrip("/").split("/", 1)[0]


_REST_SEGMENT = _segment(_get_rest_path("/probe"))
_AJAX_SEGMENT = _segment(_get_ajax_path("/probe"))

# Full path prefixes, e.g. "/api" and "/ajax-api" (or "/mlflow/api" under a
# static prefix).
REST_PATH_PREFIX = f"{_STATIC_PREFIX}/{_REST_SEGMENT}"
AJAX_PATH_PREFIX = f"{_STATIC_PREFIX}/{_AJAX_SEGMENT}"

# Both prefixes carry programmatic (XHR / API-client) traffic, so a failed
# request under either must surface as an HTTP error rather than a redirect.
API_PATH_PREFIXES = (REST_PATH_PREFIX, AJAX_PATH_PREFIX)


def to_ajax_path(path: str) -> Optional[str]:
    """Return the "/ajax-api" twin of an "/api" path.

    Args:
        path: A route path, e.g. "/api/2.0/mlflow/users/current".

    Returns:
        The equivalent "/ajax-api" path, or None when `path` is not under the
        REST prefix at all (health checks and the "/oidc/*" routes), since those
        have no UI-facing twin.
    """
    rest_prefix = f"{REST_PATH_PREFIX}/"
    if not path.startswith(rest_prefix):
        return None
    return f"{AJAX_PATH_PREFIX}/{path[len(rest_prefix):]}"


EXPERIMENT_PERMISSIONS_ROUTER_PREFIX = _get_rest_path("/mlflow/permissions/experiments")
GROUP_PERMISSIONS_ROUTER_PREFIX = _get_rest_path("/mlflow/permissions/groups")
PROMPT_PERMISSIONS_ROUTER_PREFIX = _get_rest_path("/mlflow/permissions/prompts")
REGISTERED_MODEL_PERMISSIONS_ROUTER_PREFIX = _get_rest_path("/mlflow/permissions/registered-models")
GATEWAY_PERMISSIONS_ROUTER_PREFIX = _get_rest_path("/mlflow/permissions/gateways")
MCP_SERVER_PERMISSIONS_ROUTER_PREFIX = _get_rest_path("/mlflow/permissions/mcp-servers")
USER_PERMISSIONS_ROUTER_PREFIX = _get_rest_path("/mlflow/permissions/users")
SCORERS_ROUTER_PREFIX = _get_rest_path("/mlflow/permissions/scorers", version=3)
USERS_ROUTER_PREFIX = _get_rest_path("/mlflow/users")
HEALTH_CHECK_ROUTER_PREFIX = "/health"
UI_ROUTER_PREFIX = "/oidc/ui"
TRASH_ROUTER_PREFIX = "/oidc/trash"
WEBHOOK_ROUTER_PREFIX = "/oidc/webhook"
WORKSPACE_PERMISSIONS_ROUTER_PREFIX = _get_rest_path("/mlflow/permissions/workspaces", version=3)
WORKSPACE_REGEX_PERMISSIONS_ROUTER_PREFIX = _get_rest_path("/mlflow/permissions/workspaces/regex", version=3)
WORKSPACE_RULES_ROUTER_PREFIX = _get_rest_path("/mlflow/workspace-rules", version=3)
# SCIM 2.0 (RFC 7644) lives at the conventional root path rather than under MLflow's REST prefix:
# directories are configured with a base URL ending in /scim/v2, and it has no "/ajax-api" twin.
SCIM_ROUTER_PREFIX = "/scim/v2"
SCIM_TOKENS_ROUTER_PREFIX = _get_rest_path("/mlflow/scim/tokens")
# Provisioning status and the SCIM activity log (#325), admin only.
SCIM_ADMIN_ROUTER_PREFIX = _get_rest_path("/mlflow/scim")
