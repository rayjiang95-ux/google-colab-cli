# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.client import (
    CredentialsPropagationError,
    CredentialsPropagationResult,
)
from colab_cli.commands.automation import (
    AutomationExecutionError,
    INTERACTIVE_AUTOMATION_TIMEOUT_SEC,
    build_colab_input_reply,
    describe_authorization_uri,
)
from colab_cli.state import SessionState


runner = CliRunner()


@pytest.fixture
def mock_session():
    return SessionState(
        name="test-session",
        token="test-token",
        url="https://test.url",
        endpoint="e1",
    )


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_cli_auth(mock_state, mock_runtime_class, mock_session):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Success"}]

    result = runner.invoke(app, ["auth", "-s", "test-session"])
    assert result.exit_code == 0

    assert mock_session.last_execution[0] == "automation:auth"
    assert mock_session.last_execution[1] is None
    assert mock_session.last_execution[2] is not None
    mock_state.store.add.assert_called_with(mock_session)

    # Verify ColabRuntime was invoked with the correct code
    mock_runtime.execute_code.assert_called_once()
    called_code = mock_runtime.execute_code.call_args[0][0]

    assert "os.environ['USE_AUTH_EPHEM'] = '0'" in called_code
    assert "auth.authenticate_user()" in called_code


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_cli_install(mock_state, mock_runtime_class, mock_session):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Installed"}]

    result = runner.invoke(app, ["install", "-s", "test-session", "pandas", "numpy"])
    assert result.exit_code == 0
    assert mock_session.last_execution[0] == "automation:install"
    assert mock_session.last_execution[2] is not None
    mock_state.store.add.assert_called_with(mock_session)

    mock_runtime.execute_code.assert_called_once()
    called_code = mock_runtime.execute_code.call_args[0][0]

    assert "subprocess" in called_code
    assert "pip" in called_code
    assert "pandas" in called_code
    assert "numpy" in called_code


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_cli_drivemount(mock_state, mock_runtime_class, mock_session):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Mounted"}]

    result = runner.invoke(app, ["drivemount", "-s", "test-session", "/foo/bar"])
    assert result.exit_code == 0

    # Verify ColabRuntime was invoked with the correct code
    mock_runtime.execute_code.assert_called_once()
    called_code = mock_runtime.execute_code.call_args[0][0]

    assert "drive.mount('/foo/bar')" in called_code
    assert mock_runtime.colab_request_hook is not None
    # Drivemount waits for the user to OAuth in their browser; the kernel
    # goes silent during that wait and the default 10s execute() timeout
    # would raise TimeoutError mid-flow. Insist on a generous timeout
    # (>= 5 minutes) being forwarded to runtime.execute_code.
    _, kwargs = mock_runtime.execute_code.call_args
    assert kwargs.get("timeout") is not None and kwargs["timeout"] >= 300


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_cli_auth_uses_long_timeout(mock_state, mock_runtime_class, mock_session):
    """`colab auth` walks the user through a paste-the-code flow that
    routinely takes >10s, so it must pass a generous timeout to
    runtime.execute_code or the call will TimeoutError mid-flow."""
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Authenticated"}]

    result = runner.invoke(app, ["auth", "-s", "test-session"])
    assert result.exit_code == 0

    _, kwargs = mock_runtime.execute_code.call_args
    assert kwargs.get("timeout") is not None and kwargs["timeout"] >= 300


def test_build_colab_input_reply_success_contract():
    reply = build_colab_input_reply(
        client_session_id="client-session",
        colab_msg_id=123,
    )

    assert reply["header"]["msg_type"] == "input_reply"
    assert reply["header"]["session"] == "client-session"
    assert reply["header"]["username"] == "username"
    assert reply["header"]["version"] == "5.0"
    assert reply["content"]["value"] == {
        "type": "colab_reply",
        "colab_msg_id": 123,
    }
    assert reply["channel"] == "stdin"
    assert reply["metadata"] == {}
    assert reply["parent_header"] == {}


def test_build_colab_input_reply_failure_is_redacted():
    reply = build_colab_input_reply(
        client_session_id="client-session",
        colab_msg_id=456,
        error=(
            "token=secret-token "
            "https://accounts.google.com/o/oauth2/auth?code=secret-code"
        ),
    )

    value = reply["content"]["value"]
    assert value["colab_msg_id"] == 456
    assert value["error"]
    assert "secret-token" not in value["error"]
    assert "secret-code" not in value["error"]
    assert "?" not in value["error"]


def test_describe_authorization_uri_omits_query_values():
    description = describe_authorization_uri(
        "https://accounts.google.com/o/oauth2/auth"
        "?client_id=secret-client&code=secret-code&scope=drive"
    )

    assert description == {
        "scheme": "https",
        "hostname": "accounts.google.com",
        "path": "/o/oauth2/auth",
        "query_parameter_names": ["client_id", "code", "scope"],
    }
    serialized = repr(description)
    assert "secret-client" not in serialized
    assert "secret-code" not in serialized


@pytest.mark.parametrize("client_session_id", ["", None])
def test_build_colab_input_reply_fails_closed_without_client_session(
    client_session_id,
):
    with pytest.raises(
        AutomationExecutionError, match="Jupyter client session ID is unavailable"
    ):
        build_colab_input_reply(
            client_session_id=client_session_id,
            colab_msg_id=123,
        )


def colab_request(msg_id=123):
    return {
        "header": {"msg_type": "colab_request", "session": "kernel-session"},
        "content": {"request": {"authType": "dfs_ephemeral"}},
        "metadata": {
            "colab_request_type": "request_auth",
            "colab_msg_id": msg_id,
        },
    }


def wsclient(client_session_id="client-session"):
    from unittest.mock import MagicMock

    client = MagicMock()
    client.session.session = client_session_id
    return client


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_drivemount_propagation_success_resumes_with_custom_reply(
    mock_state, mock_runtime_class, mock_session
):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"
    mock_state.client.propagate_credentials.side_effect = [
        CredentialsPropagationResult(success=True),
        CredentialsPropagationResult(success=True),
    ]
    mock_runtime = mock_runtime_class.return_value
    client = wsclient()

    def execute_code(*_args, **_kwargs):
        assert mock_runtime.colab_request_hook(colab_request(), client) is True
        return [{"text": "Mounted"}]

    mock_runtime.execute_code.side_effect = execute_code

    result = runner.invoke(app, ["drivemount", "-s", "test-session"])

    assert result.exit_code == 0
    assert "Credentials propagated. Resuming mount" in result.stdout
    assert mock_state.client.propagate_credentials.call_count == 2
    dry_call, final_call = mock_state.client.propagate_credentials.call_args_list
    assert dry_call.kwargs == {
        "auth_type": "dfs_ephemeral",
        "dry_run": True,
    }
    assert final_call.kwargs == {
        "auth_type": "dfs_ephemeral",
        "dry_run": False,
    }
    reply = client.stdin_channel.send.call_args.args[0]
    assert reply["header"]["session"] == "client-session"
    assert reply["content"]["value"]["colab_msg_id"] == 123
    assert reply["parent_header"] == {}


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_drivemount_propagation_failure_sends_failure_reply_and_exits_nonzero(
    mock_state, mock_runtime_class, mock_session
):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"
    mock_state.client.propagate_credentials.side_effect = [
        CredentialsPropagationResult(success=True),
        CredentialsPropagationError("Credentials propagation unsuccessful"),
    ]
    mock_runtime = mock_runtime_class.return_value
    client = wsclient()

    def execute_code(*_args, **_kwargs):
        assert mock_runtime.colab_request_hook(colab_request(), client) is True
        return []

    mock_runtime.execute_code.side_effect = execute_code

    result = runner.invoke(app, ["drivemount", "-s", "test-session"])

    assert result.exit_code != 0
    assert "Credentials propagated. Resuming mount" not in result.stdout
    reply = client.stdin_channel.send.call_args.args[0]
    assert reply["content"]["value"]["error"]
    assert reply["parent_header"] == {}


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_failure_history_error_still_sends_one_redacted_failure_reply(
    mock_state, mock_runtime_class, mock_session
):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"
    mock_state.client.propagate_credentials.side_effect = [
        CredentialsPropagationResult(success=True),
        CredentialsPropagationError(
            "token=propagation-secret "
            "https://accounts.google.com/auth?code=authorization-secret"
        ),
    ]

    def log_event(_name, event, _payload):
        if event == "drive_auth_failure":
            raise RuntimeError("history-secret must not escape")

    mock_state.history.log_event.side_effect = log_event
    mock_runtime = mock_runtime_class.return_value
    client = wsclient()

    def execute_code(*_args, **_kwargs):
        assert mock_runtime.colab_request_hook(colab_request(), client) is True
        return []

    mock_runtime.execute_code.side_effect = execute_code

    result = runner.invoke(app, ["drivemount", "-s", "test-session"])

    assert result.exit_code != 0
    assert client.stdin_channel.send.call_count == 1
    reply = client.stdin_channel.send.call_args.args[0]
    assert reply["content"]["value"]["error"] == "Credentials propagation failed"
    assert reply["parent_header"] == {}
    rendered = repr(reply) + result.stdout + result.stderr
    assert "propagation-secret" not in rendered
    assert "authorization-secret" not in rendered
    assert "history-secret" not in rendered
    assert "accounts.google.com" not in rendered


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_colab_request_history_error_does_not_block_success_reply(
    mock_state, mock_runtime_class, mock_session
):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"
    mock_state.client.propagate_credentials.side_effect = [
        CredentialsPropagationResult(success=True),
        CredentialsPropagationResult(success=True),
    ]

    def log_event(_name, event, _payload):
        if event == "colab_request":
            raise RuntimeError("initial history failure")

    mock_state.history.log_event.side_effect = log_event
    mock_runtime = mock_runtime_class.return_value
    client = wsclient()

    def execute_code(*_args, **_kwargs):
        assert mock_runtime.colab_request_hook(colab_request(), client) is True
        return []

    mock_runtime.execute_code.side_effect = execute_code

    result = runner.invoke(app, ["drivemount", "-s", "test-session"])

    assert result.exit_code != 0
    assert mock_state.client.propagate_credentials.call_count == 2
    assert client.stdin_channel.send.call_count == 1
    value = client.stdin_channel.send.call_args.args[0]["content"]["value"]
    assert "error" not in value


@patch("builtins.open")
@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_drive_auth_needed_history_error_does_not_block_success_reply(
    mock_state, mock_runtime_class, mock_open, mock_session
):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"
    mock_state.client.propagate_credentials.side_effect = [
        CredentialsPropagationResult(
            success=False,
            unauthorized_redirect_uri=(
                "https://accounts.google.com/auth?client_id=fake-client"
            ),
        ),
        CredentialsPropagationResult(success=True),
    ]

    def log_event(_name, event, _payload):
        if event == "drive_auth_needed":
            raise RuntimeError("authorization history failure")

    mock_state.history.log_event.side_effect = log_event
    mock_open.return_value.__enter__.return_value.readline.return_value = "\n"
    mock_runtime = mock_runtime_class.return_value
    client = wsclient()

    def execute_code(*_args, **_kwargs):
        assert mock_runtime.colab_request_hook(colab_request(), client) is True
        return []

    mock_runtime.execute_code.side_effect = execute_code

    result = runner.invoke(app, ["drivemount", "-s", "test-session"])

    assert result.exit_code != 0
    assert mock_state.client.propagate_credentials.call_count == 2
    assert client.stdin_channel.send.call_count == 1
    value = client.stdin_channel.send.call_args.args[0]["content"]["value"]
    assert "error" not in value


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_failure_reply_send_error_is_nonzero_without_retry_or_secret_leak(
    mock_state, mock_runtime_class, mock_session
):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"
    mock_state.client.propagate_credentials.side_effect = [
        CredentialsPropagationResult(success=True),
        CredentialsPropagationError("propagation failed"),
    ]
    mock_runtime = mock_runtime_class.return_value
    client = wsclient()
    client.stdin_channel.send.side_effect = RuntimeError(
        "token=reply-secret https://accounts.google.com/auth?code=reply-code"
    )

    def execute_code(*_args, **_kwargs):
        assert mock_runtime.colab_request_hook(colab_request(), client) is True
        return []

    mock_runtime.execute_code.side_effect = execute_code

    result = runner.invoke(app, ["drivemount", "-s", "test-session"])

    assert result.exit_code != 0
    assert client.stdin_channel.send.call_count == 1
    rendered = result.stdout + result.stderr
    assert "reply-secret" not in rendered
    assert "reply-code" not in rendered
    assert "accounts.google.com" not in rendered


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_success_reply_send_error_is_nonzero_without_failure_reply_retry(
    mock_state, mock_runtime_class, mock_session
):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"
    mock_state.client.propagate_credentials.side_effect = [
        CredentialsPropagationResult(success=True),
        CredentialsPropagationResult(success=True),
    ]
    mock_runtime = mock_runtime_class.return_value
    client = wsclient()
    client.stdin_channel.send.side_effect = RuntimeError("sensitive reply failure")

    def execute_code(*_args, **_kwargs):
        assert mock_runtime.colab_request_hook(colab_request(), client) is True
        return []

    mock_runtime.execute_code.side_effect = execute_code

    result = runner.invoke(app, ["drivemount", "-s", "test-session"])

    assert result.exit_code != 0
    assert client.stdin_channel.send.call_count == 1
    assert "Credentials propagated. Resuming mount" not in result.stdout
    assert "sensitive reply failure" not in result.stderr


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_interactive_automation_history_redacts_remote_output(
    mock_state, mock_runtime_class, mock_session
):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [
        {
            "output_type": "error",
            "ename": "ValueError",
            "evalue": "token=secret-token",
            "traceback": ["https://accounts.google.com/o/oauth2/auth?code=secret-code"],
        }
    ]

    result = runner.invoke(app, ["auth", "-s", "test-session"])

    assert result.exit_code != 0
    result_event = next(
        call
        for call in mock_state.history.log_event.call_args_list
        if call.args[1] == "automation_result"
    )
    payload = repr(result_event.args[2])
    assert "secret-token" not in payload
    assert "secret-code" not in payload
    assert "accounts.google.com" not in payload
    assert payload.count("ValueError") == 1


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_success_reply_is_not_followed_by_failure_reply(
    mock_state, mock_runtime_class, mock_session
):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"
    mock_state.client.propagate_credentials.side_effect = [
        CredentialsPropagationResult(success=True),
        CredentialsPropagationResult(success=True),
    ]

    def log_event(_name, event, _payload):
        if event == "drive_auth_success":
            raise RuntimeError("token=success-history-secret")

    mock_state.history.log_event.side_effect = log_event
    mock_runtime = mock_runtime_class.return_value
    client = wsclient()

    def execute_code(*_args, **_kwargs):
        assert mock_runtime.colab_request_hook(colab_request(), client) is True
        return []

    mock_runtime.execute_code.side_effect = execute_code

    result = runner.invoke(app, ["drivemount", "-s", "test-session"])

    assert result.exit_code != 0
    assert client.stdin_channel.send.call_count == 1
    assert (
        "error" not in client.stdin_channel.send.call_args.args[0]["content"]["value"]
    )
    assert "success-history-secret" not in result.stderr


@pytest.mark.parametrize("command", ["drivemount", "auth"])
@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_automation_remote_error_exits_nonzero(
    mock_state, mock_runtime_class, mock_session, command
):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [
        {
            "output_type": "error",
            "ename": "ValueError",
            "evalue": "remote failed",
            "traceback": ["ValueError: remote failed"],
        }
    ]

    result = runner.invoke(app, [command, "-s", "test-session"])

    assert result.exit_code != 0
    assert f"Remote {command} failed (ValueError)" in result.stderr


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_drivemount_success_still_exits_zero(
    mock_state, mock_runtime_class, mock_session
):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Mounted"}]

    result = runner.invoke(app, ["drivemount", "-s", "test-session"])

    assert result.exit_code == 0


def test_interactive_automation_timeout_remains_600_seconds():
    assert INTERACTIVE_AUTOMATION_TIMEOUT_SEC == 600
