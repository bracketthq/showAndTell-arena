"""Idempotent, SQL-free bootstrap for the pinned Twenty CRM fixture."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


API_KEY_NAME = "showAndTell-fixture"
API_KEY_EXPIRES_AT = "2099-12-31T00:00:00.000Z"
DEMO_WORKSPACE_MEMBERS = (
    ("bernie.osei@showAndTell.test", "Bernie", "Osei"),
    ("john.whitaker@showAndTell.test", "John", "Whitaker"),
)
DEMO_MEMBER_PASSWORD = "ShowAndTell-Twenty1!"


class TwentyBootstrapError(RuntimeError):
    """An actionable bootstrap failure with no token material in its message."""


@dataclass(frozen=True, slots=True)
class TwentyBootstrapResult:
    api_token: str
    workspace_id: str
    workspace_member_id: str


class _MetadataClient:
    def __init__(self, base_url: str, *, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        token: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self.client.post(
                f"{self.base_url}/metadata",
                headers=headers,
                json={"query": query, "variables": variables or {}},
            )
        except httpx.HTTPError as exc:
            raise TwentyBootstrapError("Twenty metadata endpoint is unreachable") from exc
        if response.status_code != 200:
            raise TwentyBootstrapError(
                f"Twenty metadata operation failed with HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TwentyBootstrapError("Twenty metadata response was not JSON") from exc
        if not isinstance(payload, dict):
            raise TwentyBootstrapError("Twenty metadata response was malformed")
        # GraphQL may return useful-looking data alongside errors. Treat any error
        # array as a failed operation so bootstrap can never bank partial state.
        errors = payload.get("errors")
        if errors:
            messages = [
                item.get("message", "unknown GraphQL error")
                for item in errors
                if isinstance(item, dict)
            ]
            summary = "; ".join(messages)[:500] or "unknown GraphQL error"
            raise TwentyBootstrapError(f"Twenty metadata operation failed: {summary}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise TwentyBootstrapError("Twenty metadata response had no data object")
        return data


def _token_pair(data: dict[str, Any], field: str) -> str:
    try:
        value = data[field]["tokens"]["accessOrWorkspaceAgnosticToken"]["token"]
    except (KeyError, TypeError) as exc:
        raise TwentyBootstrapError(f"Twenty {field} returned no access token") from exc
    if not isinstance(value, str) or not value:
        raise TwentyBootstrapError(f"Twenty {field} returned a malformed access token")
    return value


def _workspace_token(client: _MetadataClient, email: str, password: str) -> str | None:
    # Twenty canonicalizes signup addresses to lowercase in ``core.user`` but
    # its metadata login compares the supplied address case-sensitively.  Use
    # the same canonical form on every start so a workspace created from a
    # mixed-case manifest remains reusable.
    email = email.strip().casefold()
    query = """
      mutation Login($email: String!, $password: String!, $origin: String!) {
        getLoginTokenFromCredentials(email: $email, password: $password, origin: $origin) {
          loginToken { token }
        }
      }
    """
    try:
        data = client.graphql(
            query,
            {"email": email, "password": password, "origin": client.base_url},
        )
    except TwentyBootstrapError as exc:
        if any(
            marker in str(exc).lower()
            for marker in ("invalid credentials", "user not found", "workspace not found")
        ):
            return None
        raise
    try:
        login_token = data["getLoginTokenFromCredentials"]["loginToken"]["token"]
    except (KeyError, TypeError) as exc:
        raise TwentyBootstrapError("Twenty login returned no login token") from exc
    data = client.graphql(
        """
          mutation Tokens($loginToken: String!, $origin: String!) {
            getAuthTokensFromLoginToken(loginToken: $loginToken, origin: $origin) {
              tokens { accessOrWorkspaceAgnosticToken { token } }
            }
          }
        """,
        {"loginToken": login_token, "origin": client.base_url},
    )
    return _token_pair(data, "getAuthTokensFromLoginToken")


def _create_workspace_token(
    client: _MetadataClient, email: str, password: str, workspace_name: str
) -> str:
    email = email.strip().casefold()
    sign_up = client.graphql(
        """
          mutation SignUp($email: String!, $password: String!) {
            signUp(email: $email, password: $password) {
              tokens { accessOrWorkspaceAgnosticToken { token } }
            }
          }
        """,
        {"email": email, "password": password},
    )
    agnostic_token = _token_pair(sign_up, "signUp")
    created = client.graphql(
        """
          mutation CreateWorkspace {
            signUpInNewWorkspace { loginToken { token } workspace { id } }
          }
        """,
        token=agnostic_token,
    )
    try:
        login_token = created["signUpInNewWorkspace"]["loginToken"]["token"]
    except (KeyError, TypeError) as exc:
        raise TwentyBootstrapError("Twenty workspace creation returned no login token") from exc
    tokens = client.graphql(
        """
          mutation Tokens($loginToken: String!, $origin: String!) {
            getAuthTokensFromLoginToken(loginToken: $loginToken, origin: $origin) {
              tokens { accessOrWorkspaceAgnosticToken { token } }
            }
          }
        """,
        {"loginToken": login_token, "origin": client.base_url},
    )
    token = _token_pair(tokens, "getAuthTokensFromLoginToken")
    client.graphql(
        """
          mutation Activate($input: ActivateWorkspaceInput!) {
            activateWorkspace(data: $input) { id }
          }
        """,
        {"input": {"displayName": workspace_name}},
        token=token,
    )
    return token


def _current_identity(client: _MetadataClient, token: str) -> tuple[str, str]:
    data = client.graphql(
        """
          query CurrentIdentity {
            currentUser {
              workspaceMember { id }
              currentWorkspace { id }
            }
          }
        """,
        token=token,
    )
    try:
        user = data["currentUser"]
        return str(user["currentWorkspace"]["id"]), str(user["workspaceMember"]["id"])
    except (KeyError, TypeError) as exc:
        raise TwentyBootstrapError("Twenty returned no current workspace identity") from exc


def _complete_onboarding(
    client: _MetadataClient, token: str, workspace_member_id: str
) -> None:
    client.graphql(
        """
          mutation Profile($input: UpdateWorkspaceMemberSettingsInput!) {
            updateWorkspaceMemberSettings(input: $input)
          }
        """,
        {
            "input": {
                "workspaceMemberId": workspace_member_id,
                "update": {
                    "name": {"firstName": "ShowAndTell", "lastName": "Operator"},
                    "colorScheme": "System",
                },
            }
        },
        token=token,
    )
    client.graphql(
        """
          mutation SkipSyncEmailOnboardingStep {
            skipSyncEmailOnboardingStep { success }
          }
        """,
        token=token,
    )
    # The visible onboarding "Skip" action deliberately sends an empty list;
    # it clears the pending invite step without producing email or a second user.
    client.graphql(
        """
          mutation FinishInvites($emails: [String!]!) {
            sendInvitations(emails: $emails) { success errors }
          }
        """,
        {"emails": []},
        token=token,
    )


def _ensure_demo_workspace_members(
    client: _MetadataClient, token: str, workspace_id: str
) -> None:
    """Ensure the stable assignees used by captured CRM workflows exist.

    Twenty's upstream sample workspace included these collaborators, but the
    pinned local fixture starts with only its operator.  Create the two demo
    members through Twenty's public-invite and profile APIs so task replays can
    assign records without database fixtures or synthetic UI options.
    """

    workspace = client.graphql(
        """
          query CurrentWorkspaceInvite {
            currentWorkspace { id inviteHash isPublicInviteLinkEnabled }
          }
        """,
        token=token,
    ).get("currentWorkspace")
    if not isinstance(workspace, dict) or str(workspace.get("id")) != workspace_id:
        raise TwentyBootstrapError("Twenty returned an unexpected workspace invite")
    invite_hash = workspace.get("inviteHash")
    if workspace.get("isPublicInviteLinkEnabled") is not True:
        raise TwentyBootstrapError("Twenty fixture public invite link is disabled")
    if not isinstance(invite_hash, str) or not invite_hash:
        raise TwentyBootstrapError("Twenty fixture returned no workspace invite hash")

    for email, first_name, last_name in DEMO_WORKSPACE_MEMBERS:
        member_token = _workspace_token(client, email, DEMO_MEMBER_PASSWORD)
        if member_token is None:
            signed_up = client.graphql(
                """
                  mutation JoinFixtureWorkspace(
                    $email: String!,
                    $password: String!,
                    $workspaceId: UUID!,
                    $workspaceInviteHash: String!
                  ) {
                    signUpInWorkspace(
                      email: $email,
                      password: $password,
                      workspaceId: $workspaceId,
                      workspaceInviteHash: $workspaceInviteHash
                    ) { loginToken { token } workspace { id } }
                  }
                """,
                {
                    "email": email,
                    "password": DEMO_MEMBER_PASSWORD,
                    "workspaceId": workspace_id,
                    "workspaceInviteHash": invite_hash,
                },
            )
            try:
                login_token = signed_up["signUpInWorkspace"]["loginToken"]["token"]
            except (KeyError, TypeError) as exc:
                raise TwentyBootstrapError(
                    f"Twenty did not create demo member {email!r}"
                ) from exc
            tokens = client.graphql(
                """
                  mutation Tokens($loginToken: String!, $origin: String!) {
                    getAuthTokensFromLoginToken(
                      loginToken: $loginToken,
                      origin: $origin
                    ) { tokens { accessOrWorkspaceAgnosticToken { token } } }
                  }
                """,
                {"loginToken": login_token, "origin": client.base_url},
            )
            member_token = _token_pair(tokens, "getAuthTokensFromLoginToken")

        member_workspace_id, member_id = _current_identity(client, member_token)
        if member_workspace_id != workspace_id:
            raise TwentyBootstrapError(
                f"Twenty demo member {email!r} joined an unexpected workspace"
            )
        client.graphql(
            """
              mutation Profile($input: UpdateWorkspaceMemberSettingsInput!) {
                updateWorkspaceMemberSettings(input: $input)
              }
            """,
            {
                "input": {
                    "workspaceMemberId": member_id,
                    "update": {
                        "name": {
                            "firstName": first_name,
                            "lastName": last_name,
                        },
                        "colorScheme": "System",
                    },
                }
            },
            token=member_token,
        )
def _ensure_api_key(client: _MetadataClient, token: str) -> str:
    roles = client.graphql(
        """
          query Roles { getRoles { id label canBeAssignedToApiKeys canUpdateAllSettings } }
        """,
        token=token,
    ).get("getRoles")
    if not isinstance(roles, list):
        raise TwentyBootstrapError("Twenty returned malformed workspace roles")
    assignable = [
        role for role in roles
        if isinstance(role, dict) and role.get("canBeAssignedToApiKeys") is True
    ]
    if not assignable:
        raise TwentyBootstrapError("Twenty has no role assignable to API keys")
    role = next((item for item in assignable if item.get("canUpdateAllSettings")), assignable[0])

    keys = client.graphql(
        "query ApiKeys { apiKeys { id name expiresAt revokedAt } }", token=token
    ).get("apiKeys")
    if not isinstance(keys, list):
        raise TwentyBootstrapError("Twenty returned malformed API keys")
    matches = [item for item in keys if isinstance(item, dict) and item.get("name") == API_KEY_NAME]
    if len(matches) > 1:
        raise TwentyBootstrapError("Twenty has duplicate ShowAndTell API keys")
    if matches and matches[0].get("revokedAt") is None:
        key = matches[0]
    else:
        created = client.graphql(
            """
              mutation CreateApiKey($input: CreateApiKeyInput!) {
                createApiKey(input: $input) { id expiresAt }
              }
            """,
            {
                "input": {
                    "name": API_KEY_NAME,
                    "expiresAt": API_KEY_EXPIRES_AT,
                    "roleId": role["id"],
                }
            },
            token=token,
        )
        key = created.get("createApiKey")
    if not isinstance(key, dict) or not isinstance(key.get("id"), str):
        raise TwentyBootstrapError("Twenty did not create a usable API key")
    expires_at = key.get("expiresAt") or API_KEY_EXPIRES_AT
    generated = client.graphql(
        """
          mutation Generate($apiKeyId: UUID!, $expiresAt: String!) {
            generateApiKeyToken(apiKeyId: $apiKeyId, expiresAt: $expiresAt) { token }
          }
        """,
        {"apiKeyId": key["id"], "expiresAt": expires_at},
        token=token,
    )
    try:
        api_token = generated["generateApiKeyToken"]["token"]
    except (KeyError, TypeError) as exc:
        raise TwentyBootstrapError("Twenty did not generate an API token") from exc
    if not isinstance(api_token, str) or not api_token:
        raise TwentyBootstrapError("Twenty generated a malformed API token")
    return api_token


def bootstrap_twenty(
    *, base_url: str, email: str, password: str, workspace_name: str = "ShowAndTell CRM"
) -> TwentyBootstrapResult:
    """Create or reuse the fixture workspace and return a fresh API token."""
    client = _MetadataClient(base_url)
    try:
        token = _workspace_token(client, email, password)
        if token is None:
            token = _create_workspace_token(client, email, password, workspace_name)
        workspace_id, member_id = _current_identity(client, token)
        _complete_onboarding(client, token, member_id)
        _ensure_demo_workspace_members(client, token, workspace_id)
        api_token = _ensure_api_key(client, token)
        return TwentyBootstrapResult(api_token, workspace_id, member_id)
    finally:
        client.close()
