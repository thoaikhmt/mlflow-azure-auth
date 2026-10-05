import json
import os

from flask import Response, request

from mlflow_oidc_auth.config import config
from mlflow_oidc_auth.logger import get_logger

logger = get_logger()

_BODY_CLOSE_TAG = "</body>"
_HACK_DIR = os.path.join(os.path.dirname(__file__), "hack")

# ``menu.html`` / ``reauth.html`` build links to the plugin's fixed routes
# (/oidc/ui, /login, /logout). Those routes live under the deployment's base path,
# which is only known per request, so the snippets carry this token and the server
# substitutes it below.
_BASE_PATH_TOKEN = "__OIDC_BASE_PATH__"

# ``default-model.html`` needs the configured default judge model as a JavaScript
# string literal. The server substitutes this token with the JSON-encoded value
# (or ``null`` when unset), so the page can read it without a server round-trip.
_DEFAULT_MODEL_TOKEN = "__OIDC_DEFAULT_JUDGE_MODEL__"


def _request_base_path() -> str:
    """Return the deployment base path (ASGI ``root_path``) for the current request.

    Flask sees the ASGI ``root_path`` as ``SCRIPT_NAME`` (``asgiref`` copies it there),
    so a deployment mounted under ``/mlflow`` yields ``/mlflow`` here. With MLflow's
    ``--static-prefix`` the prefix is baked into the route instead and this is empty,
    which is correct: the plugin's own routes stay at the server root. A candidate
    that is not a plain path is dropped (mirrors ``utils.get_base_path``), so a broken
    value never produces a link that leaves the origin.
    """
    try:
        script_root = request.script_root or ""
    except RuntimeError:  # no request context
        return ""
    if not script_root.startswith("/") or script_root.startswith("//"):
        return ""
    return script_root.rstrip("/")


def _read_snippet(name: str) -> str:
    """Read a snippet from the hack/ directory. Returns empty string if missing."""

    path = os.path.join(_HACK_DIR, name)
    if not os.path.exists(path):
        logger.warning("Injection snippet '%s' not found at %s; skipping", name, path)
        return ""
    with open(path, "r") as f:
        return f.read()


def index():
    import textwrap

    from mlflow.server import app

    static_folder = app.static_folder

    text_notfound = textwrap.dedent("Unable to display MLflow UI - landing page not found")
    text_notset = textwrap.dedent("Static folder is not set")

    if static_folder is None:
        return Response(text_notset, mimetype="text/plain")

    index_path = os.path.join(static_folder, "index.html")

    if not os.path.exists(index_path):
        return Response(text_notfound, mimetype="text/plain")

    with open(index_path, "r") as f:
        html_content = f.read()

    if _BODY_CLOSE_TAG not in html_content:
        logger.warning(
            "MLflow index.html does not contain '%s' marker; injection skipped",
            _BODY_CLOSE_TAG,
        )
        return html_content

    # Build the combined injection. Re-auth runs first so the fetch/XHR patch is
    # in place before the menu code (or any later script) issues network calls.
    injections = []
    if config.EXTEND_MLFLOW_REAUTH:
        injections.append(_read_snippet("reauth.html"))
    if config.EXTEND_MLFLOW_MENU:
        injections.append(_read_snippet("menu.html"))
    # Only inject when a default is configured; otherwise there is nothing to
    # pre-select and the page stays untouched.
    if config.EXTEND_MLFLOW_DEFAULT_MODEL and config.MLFLOW_GENAI_JUDGE_DEFAULT_MODEL:
        injections.append(_read_snippet("default-model.html"))

    injected = "\n".join(s for s in injections if s)
    if not injected:
        return html_content

    # JSON-encode (dropping the surrounding quotes) so the value is safe inside the
    # snippets' double-quoted JavaScript strings whatever the prefix contains. The
    # ``<`` escape keeps a ``</script>`` in the prefix from closing the inline block.
    base_path_js = json.dumps(_request_base_path())[1:-1].replace("<", "\\u003c")
    injected = injected.replace(_BASE_PATH_TOKEN, base_path_js)

    # Keep the quotes: the snippet assigns the token to a JavaScript string. Escape
    # ``<`` so a ``</script>`` in the configured value cannot close the inline block.
    default_model = config.MLFLOW_GENAI_JUDGE_DEFAULT_MODEL or None
    default_model_js = json.dumps(default_model).replace("<", "\\u003c")
    injected = injected.replace(_DEFAULT_MODEL_TOKEN, default_model_js)

    return html_content.replace(_BODY_CLOSE_TAG, f"{injected}\n{_BODY_CLOSE_TAG}")
