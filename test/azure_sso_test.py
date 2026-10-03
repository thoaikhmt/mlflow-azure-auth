#!/usr/bin/env python3
"""Integration test: MLflow `oidc-auth` against a simulated Azure Entra ID.

Runs against the containers started by ``test/run-integration.sh`` (or ``docker
compose -f docker-compose.test.yml``). It drives the real OpenID Connect
Authorization Code + PKCE flow through the mock Entra ID login page and asserts
that MLflow:

  * advertises the Azure provider on its login page,
  * starts login with PKCE (S256),
  * provisions the user and applies the group gate,
  * grants admin from the Entra admin group,
  * refuses a user who is in no allowed group,
  * rejects unauthenticated API traffic.

The mock maps the login *username* to Entra claims (`groups`, `email`, ...) via
``mock-azure/config.json``; the tenant/issuer is shaped like a real Entra ID
tenant. See the README for how to point the same config at a real tenant.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse

import requests

MLFLOW = os.environ.get("MLFLOW_BASE", "http://mlflow:5000")
# MLflow may run behind `--static-prefix` (the k3s deployment uses `/mlflow`).
# MLflow's own UI/API move under the prefix, but the oidc-auth plugin's fixed
# routes (`/login`, `/callback`, `/oidc/ui`, `/providers`, `/auth/status`) stay
# at the server root, so the test uses two bases.
STATIC_PREFIX = os.environ.get("MLFLOW_STATIC_PREFIX", "").rstrip("/")
API_BASE = f"{MLFLOW}{STATIC_PREFIX}"
PROVIDER = os.environ.get("OIDC_PROVIDER", "default")
# Entra ID sends the group *object IDs* (GUIDs) in the `groups` claim.
ALLOWED_GROUP = os.environ.get("OIDC_GROUP_NAME", "22222222-2222-2222-2222-222222222222")
ADMIN_GROUP = os.environ.get("OIDC_ADMIN_GROUP_NAME", "33333333-3333-3333-3333-333333333333")
# The plugin is configured with OIDC_USERNAME_FIELD=oid, so the account name is
# the immutable Entra object id, not the email/UPN.
ALICE_USERNAME = os.environ.get("EXPECTED_USERNAME", "44444444-4444-4444-4444-444444444444")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "55555555-5555-5555-5555-555555555555")
CAROL_USERNAME = os.environ.get("CAROL_USERNAME", "77777777-7777-7777-7777-777777777777")
# A group Carol belongs to that is NOT in the allowed list.
UNRELATED_GROUP = "88888888-8888-8888-8888-888888888888"
DISPLAY_NAME = os.environ.get("OIDC_PROVIDER_DISPLAY_NAME", "Sign in with Azure Entra ID")

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        _failures.append(f"{label}: {detail}")


def start_login(username: str) -> tuple[requests.Session, requests.Response]:
    """Run the Authorization Code + PKCE flow for ``username``.

    Returns the session (with the MLflow cookie) and the final callback response.
    """
    session = requests.Session()

    resp = session.get(f"{MLFLOW}/login/{PROVIDER}", allow_redirects=False)
    check("login redirects to the identity provider", resp.status_code == 302, f"got {resp.status_code}")
    authorize = resp.headers.get("location", "")

    parsed = urllib.parse.urlparse(authorize)
    query = urllib.parse.parse_qs(parsed.query)
    check("login uses PKCE (S256)", query.get("code_challenge_method") == ["S256"], authorize)
    check("login requests an authorization code", query.get("response_type") == ["code"], authorize)

    # The mock's interactive login page.
    page = session.get(authorize, allow_redirects=False)
    check("identity provider shows a login form", page.status_code == 200 and "<form" in page.text.lower(), f"got {page.status_code}")

    # Submit the username; the mock supplies Entra-style claims from its config.
    posted = session.post(authorize, data={"username": username, "claims": ""}, allow_redirects=False)
    check("identity provider issues a code", posted.status_code in (301, 302, 303), f"got {posted.status_code}")

    callback = posted.headers.get("location", "")
    final = session.get(callback, allow_redirects=False)
    return session, final


def auth_status(session: requests.Session) -> dict:
    resp = session.get(f"{MLFLOW}/auth/status")
    if resp.status_code != 200:
        return {"authenticated": False, "_status": resp.status_code}
    return resp.json()


def test_provider_is_advertised() -> None:
    resp = requests.get(f"{MLFLOW}/providers")
    check("providers endpoint is reachable", resp.status_code == 200, f"got {resp.status_code}")
    body = resp.json()
    providers = body.get("providers", [])
    names = [p.get("display_name") for p in providers]
    check("Entra ID provider is advertised", DISPLAY_NAME in names, json.dumps(body))


def test_local_user_login() -> None:
    session, final = start_login("alice")
    check("callback completes the login", final.status_code == 302, f"got {final.status_code} -> {final.headers.get('location')}")
    check("callback lands on the authenticated UI", "/oidc/ui/user" in final.headers.get("location", ""), final.headers.get("location", ""))

    status = auth_status(session)
    check("session is authenticated", status.get("authenticated") is True, json.dumps(status))
    check("username comes from the Entra `oid` claim", status.get("username") == ALICE_USERNAME, json.dumps(status))
    check("provider display name is reported", status.get("provider") == DISPLAY_NAME, json.dumps(status))

    current = session.get(f"{API_BASE}/api/2.0/mlflow/users/current")
    check("authenticated API call succeeds", current.status_code == 200, f"got {current.status_code}: {current.text[:120]}")
    if current.status_code == 200:
        user = current.json()
        groups = [g.get("group_name") for g in user.get("groups", [])]
        check("display name comes from the Entra `name` claim", user.get("display_name") == "Alice Example", json.dumps(user))
        check("user is in the allowed group (by object id)", ALLOWED_GROUP in groups, json.dumps(user))
        check("normal user is not an admin", user.get("is_admin") is False, json.dumps(user))

    created = session.post(f"{API_BASE}/api/2.0/mlflow/experiments/create", json={"name": "azure-sso-test"})
    check("authenticated user can create an experiment", created.status_code == 200, f"got {created.status_code}: {created.text[:120]}")


def test_admin_group_grants_admin() -> None:
    session, final = start_login("admin")
    check("admin callback completes the login", final.status_code == 302, f"got {final.status_code}")
    status = auth_status(session)
    check("admin session is authenticated", status.get("authenticated") is True, json.dumps(status))
    check("admin username is the oid", status.get("username") == ADMIN_USERNAME, json.dumps(status))

    current = session.get(f"{API_BASE}/api/2.0/mlflow/users/current")
    check("admin API call succeeds", current.status_code == 200, f"got {current.status_code}")
    if current.status_code == 200:
        check("admin group grants admin", current.json().get("is_admin") is True, current.text[:200])


def test_any_matching_group_admits() -> None:
    """A user in *several* groups is admitted if ANY of them matches the allowed list.

    ``carol`` belongs to an unrelated group and the allowed group; membership of
    the allowed group alone must let her in, and all her Entra groups are synced.
    """
    session, final = start_login("carol")
    check("multi-group callback completes the login", final.status_code == 302, f"got {final.status_code} -> {final.headers.get('location')}")
    status = auth_status(session)
    check("user with one matching group is authenticated", status.get("authenticated") is True, json.dumps(status))
    check("carol username is the oid", status.get("username") == CAROL_USERNAME, json.dumps(status))

    current = session.get(f"{API_BASE}/api/2.0/mlflow/users/current")
    check("carol API call succeeds", current.status_code == 200, f"got {current.status_code}")
    if current.status_code == 200:
        user = current.json()
        groups = [g.get("group_name") for g in user.get("groups", [])]
        check("all of carol's Entra groups are synced", ALLOWED_GROUP in groups and UNRELATED_GROUP in groups, json.dumps(groups))
        check("carol is not an admin", user.get("is_admin") is False, json.dumps(user))


def test_user_outside_allowed_group_is_refused() -> None:
    """``bob`` is in no allowed group; the group gate must refuse the login."""
    session, final = start_login("bob")
    status = auth_status(session)
    check(
        "user outside the allowed group is not authenticated",
        status.get("authenticated") is not True,
        f"final={final.status_code} status={json.dumps(status)}",
    )


def test_unauthenticated_api_is_rejected() -> None:
    resp = requests.get(f"{API_BASE}/api/2.0/mlflow/users/current")
    check("unauthenticated API call is rejected", resp.status_code == 401, f"got {resp.status_code}")


def main() -> int:
    tests = [
        test_provider_is_advertised,
        test_unauthenticated_api_is_rejected,
        test_local_user_login,
        test_admin_group_grants_admin,
        test_any_matching_group_admits,
        test_user_outside_allowed_group_is_refused,
    ]
    for test in tests:
        print(f"\n== {test.__name__} ==")
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report and keep running the rest
            check(test.__name__, False, repr(exc))

    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All integration checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
