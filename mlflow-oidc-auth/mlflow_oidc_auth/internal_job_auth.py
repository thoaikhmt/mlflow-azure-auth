"""Authentication of MLflow's own server-spawned job subprocesses.

UI-triggered jobs (GenAI evaluation, issue detection, online scoring, prompt optimization) run in
a process MLflow spawns, and talk back to the tracking server with the MLflow client. MLflow's job
functions already know how to present a credential for that: when
``_MLFLOW_INTERNAL_GATEWAY_AUTH_TOKEN`` is set, ``invoke_genai_evaluate`` copies it into
``MLFLOW_TRACKING_PASSWORD`` and the trigger user's name into ``MLFLOW_TRACKING_USERNAME`` (see
``mlflow/genai/evaluation/job.py``). MLflow only *generates* that token when the server runs its
built-in ``basic-auth`` app, so with this plugin the value is never set and every callback is
refused with "Authentication required".

This module supplies the missing half:

* :func:`install_internal_job_token` derives a stable token from ``SECRET_KEY`` and exports it as
  ``_MLFLOW_INTERNAL_GATEWAY_AUTH_TOKEN``. It runs when the plugin's app module is imported --
  which MLflow's ``run_server`` does (``_is_factory``) *before* it forks the ASGI workers and
  starts the job runner -- so the token reaches the server process, every worker and every job
  subprocess.
* :func:`is_internal_job_password` lets the authentication middleware accept that token as a
  basic-auth password and authenticate as the named user, without a token-store lookup. Normal
  per-resource permissions still apply, exactly as for that user.

The token is a server-only secret: it is never returned to a client and never logged, and its only
holders are processes the server itself started. It is compared in constant time, and a deployment
without a configured ``SECRET_KEY`` gets no token at all (fail closed) because a per-process random
key cannot be shared.
"""

from __future__ import annotations

import hmac
import os
from typing import Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mlflow_oidc_auth.config import config
from mlflow_oidc_auth.logger import get_logger

logger = get_logger()

#: Environment variable MLflow's job functions read and present as the tracking password.
MLFLOW_INTERNAL_AUTH_TOKEN_ENV = "_MLFLOW_INTERNAL_GATEWAY_AUTH_TOKEN"

#: Purpose-bound derivation label. Changing it rotates every derived token, so it is versioned
#: rather than edited in place.
_HKDF_INFO = b"mlflow-oidc-auth/internal-job-token/v1"


def _derive_token(secret_key: str) -> str:
    """HKDF-SHA256 a job token from ``SECRET_KEY``, bound to this purpose."""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO)
    return hkdf.derive(secret_key.encode("utf-8")).hex()


def _configured_or_derived_token() -> Optional[str]:
    """The token this deployment would use, ignoring any value already in the environment.

    An explicit ``OIDC_INTERNAL_AUTH_TOKEN`` wins, so an operator can pin the value (for example
    to share it with an external job runner). Otherwise it is derived from a *configured*
    ``SECRET_KEY``; with a per-process random key the derivation could not be reproduced by the
    job subprocess, so no token is offered and the feature stays off.
    """
    explicit = getattr(config, "OIDC_INTERNAL_AUTH_TOKEN", None)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if getattr(config, "SECRET_KEY_EPHEMERAL", False):
        return None
    return _derive_token(config.SECRET_KEY)


def effective_internal_token() -> Optional[str]:
    """The internal job token in force, or None when the feature is off.

    A value already in ``_MLFLOW_INTERNAL_GATEWAY_AUTH_TOKEN`` always wins: it is either set by the
    operator or inherited from the server process that installed it, and in both cases it is the
    value the job subprocess will actually present.
    """
    if not getattr(config, "OIDC_INTERNAL_AUTH_ENABLED", True):
        return None
    env_value = os.environ.get(MLFLOW_INTERNAL_AUTH_TOKEN_ENV)
    if env_value:
        return env_value
    return _configured_or_derived_token()


def install_internal_job_token() -> None:
    """Export the derived token so MLflow's job subprocesses can authenticate.

    Runs at app import, which MLflow performs in the ``mlflow server`` parent before it forks the
    ASGI workers and launches the job runner, so the exported value propagates to all of them. A
    value already present is left untouched, so an operator's explicit setting is never overridden
    and MLflow's own token (when the built-in app is in use) is respected. Idempotent.
    """
    if not getattr(config, "OIDC_INTERNAL_AUTH_ENABLED", True):
        return
    if os.environ.get(MLFLOW_INTERNAL_AUTH_TOKEN_ENV):
        return
    token = _configured_or_derived_token()
    if not token:
        if getattr(config, "SECRET_KEY_EPHEMERAL", False):
            logger.warning(
                "Internal MLflow job authentication is disabled: SECRET_KEY is not configured, so a "
                "job token cannot be shared with job subprocesses. Set SECRET_KEY to enable "
                "UI-triggered evaluation/scoring jobs, or set OIDC_INTERNAL_AUTH_TOKEN."
            )
        return
    os.environ[MLFLOW_INTERNAL_AUTH_TOKEN_ENV] = token
    logger.info("Internal MLflow job authentication enabled (MLflow job subprocesses are trusted as the requested user)")


def is_internal_job_password(presented: str) -> bool:
    """Whether ``presented`` is this deployment's internal job token.

    Compared in constant time. False when the feature is off, no token is available, or the value
    does not match, so a malformed or empty presentation can never be accepted.
    """
    token = effective_internal_token()
    if not token or not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))
