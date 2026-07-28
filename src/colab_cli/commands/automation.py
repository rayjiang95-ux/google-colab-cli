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

import datetime
import os
import re
import sys
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlsplit

import typer
from rich.console import Console
from typing_extensions import Annotated

from colab_cli.client import CredentialsPropagationError
from colab_cli.contents import ContentsClient
from colab_cli.runtime import ColabRuntime
from colab_cli.utils import render_display_data

_console = Console()


# Default execute() timeout for human-in-the-loop automations (auth /
# drivemount). The kernel goes silent while the user completes a browser
# OAuth flow, which can routinely take 30s+; the upstream 10s default
# raises ``TimeoutError`` mid-flow even though the mount actually succeeds.
# 10 minutes is long enough for any realistic interactive auth ceremony
# without leaving CI hangs unbounded.
INTERACTIVE_AUTOMATION_TIMEOUT_SEC = 600


class AutomationExecutionError(Exception):
    """A sanitized error that must make an automation command fail."""


def describe_authorization_uri(uri: str) -> Dict[str, Any]:
    """Return log-safe URL structure without query values."""
    parsed = urlsplit(uri)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CredentialsPropagationError(
            "Credentials propagation returned an invalid authorization URL"
        )
    return {
        "scheme": parsed.scheme,
        "hostname": parsed.hostname,
        "path": parsed.path,
        "query_parameter_names": sorted({name for name, _ in parse_qsl(parsed.query)}),
    }


def build_colab_input_reply(
    *,
    client_session_id: str,
    colab_msg_id: int,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Build Colab's custom stdin reply without a Jupyter parent header."""
    if not isinstance(client_session_id, str) or not client_session_id:
        raise AutomationExecutionError("Jupyter client session ID is unavailable")
    if (
        not isinstance(colab_msg_id, int)
        or isinstance(colab_msg_id, bool)
        or colab_msg_id < 0
    ):
        raise AutomationExecutionError("Colab request message ID is invalid")

    value: Dict[str, Any] = {
        "type": "colab_reply",
        "colab_msg_id": colab_msg_id,
    }
    if error is not None:
        # Never reflect arbitrary exception text, tokens, or OAuth URLs into
        # kernel output/history. The local diagnostic logs carry only the
        # controlled error category.
        value["error"] = "Credentials propagation failed"

    return {
        "header": {
            "msg_id": str(uuid.uuid4()),
            "msg_type": "input_reply",
            "session": client_session_id,
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "username": "username",
            "version": "5.0",
        },
        "content": {"value": value},
        "channel": "stdin",
        "metadata": {},
        "parent_header": {},
    }


def _send_colab_input_reply(
    wsclient, colab_msg_id: int, error: Optional[str] = None
) -> None:
    # jupyter_client.Session.session is the ID placed in header.session on
    # every outgoing execute message from this client.
    client_session_id = getattr(getattr(wsclient, "session", None), "session", None)
    reply = build_colab_input_reply(
        client_session_id=client_session_id,
        colab_msg_id=colab_msg_id,
        error=error,
    )
    wsclient.stdin_channel.send(reply)


def _raise_automation_failure(errors: List[AutomationExecutionError]):
    if errors:
        raise errors[0]


_SAFE_OUTPUT_TYPES = frozenset(
    {"display_data", "error", "execute_result", "status", "stream", "text"}
)
_SAFE_ERROR_CATEGORY = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")


def _summarize_interactive_outputs(
    outputs: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Keep interactive history useful without persisting remote payloads."""
    summary = []
    for output in outputs:
        output_type = output.get("output_type")
        if not isinstance(output_type, str) or output_type not in _SAFE_OUTPUT_TYPES:
            output_type = "unknown"
        item = {"output_type": output_type}
        if output_type == "error":
            error_category = output.get("ename")
            if not isinstance(
                error_category, str
            ) or not _SAFE_ERROR_CATEGORY.fullmatch(error_category):
                error_category = "Error"
            item["error_category"] = error_category
        summary.append(item)
    return summary


def run_automation(
    name: str,
    op: str,
    code: str,
    allow_stdin: bool = False,
    path: str = None,
    timeout: Optional[float] = None,
):
    from colab_cli.common import state

    s = state.store.get(name)
    runtime = ColabRuntime(s.url, s.token, session_name=s.name, history=state.history)
    automation_errors: List[AutomationExecutionError] = []

    def drivefs_hook(deserialize_msg, wsclient):
        content = deserialize_msg.get("content", {})
        if content.get("request", {}).get("authType") == "dfs_ephemeral":
            msg_id = deserialize_msg.get("metadata", {}).get("colab_msg_id")
            state.history.log_event(
                s.name,
                "colab_request",
                {"type": "dfs_ephemeral", "colab_msg_id": msg_id},
            )
            typer.echo(
                f"\n[colab] Intercepted Drive Auth Request. Connecting to {state.client.colab_domain}..."
            )

            try:
                dry_run_result = state.client.propagate_credentials(
                    s.endpoint,
                    auth_type="dfs_ephemeral",
                    dry_run=True,
                )
                if not dry_run_result.success:
                    uri = dry_run_result.unauthorized_redirect_uri
                    if not uri:
                        raise CredentialsPropagationError(
                            "Credentials propagation dry run did not provide authorization"
                        )
                    uri_log_fields = describe_authorization_uri(uri)
                    typer.echo(
                        "\n[colab] REQUIRED: Google Drive Authorization needed."
                        f"\nPlease visit:\n\n{uri}\n"
                    )
                    state.history.log_event(
                        s.name,
                        "drive_auth_needed",
                        uri_log_fields,
                    )
                    sys.stdout.write("Press Enter after you have granted access... ")
                    sys.stdout.flush()
                    with open("/dev/tty") as tty:
                        tty.readline()

                typer.echo("[colab] Authorizing VM...")
                state.client.propagate_credentials(
                    s.endpoint,
                    auth_type="dfs_ephemeral",
                    dry_run=False,
                )
                _send_colab_input_reply(wsclient, msg_id)
            except Exception as exc:
                error = AutomationExecutionError(
                    f"Drive credentials propagation failed ({type(exc).__name__})"
                )
                automation_errors.append(error)
                state.history.log_event(
                    s.name,
                    "drive_auth_failure",
                    {
                        "error_category": type(exc).__name__,
                        "colab_msg_id": msg_id,
                    },
                )
                try:
                    _send_colab_input_reply(wsclient, msg_id, error=str(error))
                except Exception as reply_exc:
                    automation_errors.append(
                        AutomationExecutionError(
                            "Drive credentials failure reply could not be sent "
                            f"({type(reply_exc).__name__})"
                        )
                    )
            else:
                # The success reply has already been sent. Keep later local
                # failures outside the propagation exception handler so this
                # request can never receive a second, contradictory reply.
                typer.echo("[colab] Credentials propagated. Resuming mount...")
                try:
                    state.history.log_event(s.name, "drive_auth_success", {})
                except Exception as exc:
                    automation_errors.append(
                        AutomationExecutionError(
                            f"Drive success logging failed ({type(exc).__name__})"
                        )
                    )
            return True
        return False

    runtime.colab_request_hook = drivefs_hook
    try:
        s.running = f"automation({op})"
        s.last_execution = (
            f"automation:{op}",
            None,
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        state.store.add(s)

        if op == "drivemount":
            state.history.log_event(
                name, "automation", {"op": "drivemount", "path": path, "code": code}
            )
        else:
            state.history.log_event(name, "automation", {"op": op, "code": code})

        outputs = runtime.execute_code(code, allow_stdin=allow_stdin, timeout=timeout)
        history_outputs = (
            _summarize_interactive_outputs(outputs)
            if op in {"auth", "drivemount"}
            else outputs
        )
        state.history.log_event(
            name, "automation_result", {"op": op, "outputs": history_outputs}
        )

        for out in outputs:
            if "text" in out:
                sys.stdout.write(out["text"])
            elif "data" in out:
                text = render_display_data(out["data"])
                if text is not None:
                    _console.print(text)
            elif out.get("output_type") == "error":
                ename = out.get("ename", "Error")
                if op in {"auth", "drivemount"}:
                    automation_errors.append(
                        AutomationExecutionError(f"Remote {op} failed ({ename})")
                    )
                else:
                    evalue = out.get("evalue", "")
                    tb = out.get("traceback", [])
                    if tb:
                        sys.stderr.write("".join(tb) + "\n")
                    else:
                        sys.stderr.write(f"{ename}: {evalue}\n")

        _raise_automation_failure(automation_errors)
    finally:
        s.running = None
        state.store.add(s)
        runtime.stop()


def _run_interactive_automation(*args, **kwargs) -> None:
    try:
        run_automation(*args, **kwargs)
    except AutomationExecutionError as exc:
        typer.echo(f"[colab] {exc}", err=True)
        raise typer.Exit(1) from exc


def auth(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Authenticate with Google on the VM"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    code = "import os\nos.environ['USE_AUTH_EPHEM'] = '0'\nfrom google.colab import auth\nauth.authenticate_user()"
    typer.echo(f"[colab] Starting Google Auth flow on {name}...")
    _run_interactive_automation(
        name,
        "auth",
        code,
        allow_stdin=True,
        timeout=INTERACTIVE_AUTOMATION_TIMEOUT_SEC,
    )


def drivemount(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    path: Annotated[str, typer.Argument(help="Mount path")] = "/content/drive",
):
    """Mount Google Drive at path"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    code = f"from google.colab import drive\ndrive.mount('{path}')"
    typer.echo(f"[colab] Mounting Google Drive to '{path}' on {name}...")
    _run_interactive_automation(
        name,
        "drivemount",
        code,
        allow_stdin=True,
        path=path,
        timeout=INTERACTIVE_AUTOMATION_TIMEOUT_SEC,
    )


def install(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    packages: Annotated[
        Optional[List[str]], typer.Argument(help="Packages to install")
    ] = None,
    requirement: Annotated[
        Optional[str], typer.Option("-r", "--requirement", help="Requirements file")
    ] = None,
):
    """Install python packages on the VM"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    if not packages and not requirement:
        typer.echo("[colab] No packages or requirements specified.")
        raise typer.Exit(1)

    commands = []
    if requirement:
        if not os.path.isfile(requirement):
            typer.echo(f"[colab] Requirements file '{requirement}' not found locally.")
            raise typer.Exit(1)
        contents = ContentsClient(state.store.get(name))
        remote_path = f"content/{os.path.basename(requirement)}"
        contents.upload(requirement, remote_path)
        commands.extend(["-r", f"/{remote_path}"])
    if packages:
        commands.extend(packages)

    cmd_str = ", ".join(f"'{c}'" for c in commands)
    code = f"""
import subprocess, sys
def install():
    packages = [{cmd_str}]
    try:
        subprocess.check_call(['uv', 'pip', 'install', '--system'] + packages)
        print('Installation Complete (via uv)!')
    except:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install'] + packages)
        print('Installation Complete (via pip)!')
install()
"""
    typer.echo(f"[colab] Installing packages on {name} (preferring uv)...")
    run_automation(name, "install", code)


def register(app: typer.Typer):
    app.command(hidden=True)(auth)
    app.command()(drivemount)
    app.command()(install)
