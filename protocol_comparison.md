# DriveFS credential-propagation protocol comparison

Compared sources:

- `googlecolab/google-colab-cli` at
  `1bdb55b6dc3c6ad2300a62305140e17cb25933d6`
- `googlecolab/colab-vscode` at
  `af9192a1b3283d8ffb9ba44c10af1caf7f3d6727`

The comparison covers the CLI's `client.py`, `commands/automation.py`,
`runtime.py`, and `auth.py`, and the extension's `src/auth/ephemeral.ts`,
`src/colab/client/v1/index.ts`, and
`src/jupyter/colab-proxy-websocket.ts`.

| Protocol element | CLI before this change | Supported `colab-vscode` behavior | CLI after this change |
| --- | --- | --- | --- |
| Dry-run request | One token GET followed by a POST with an old multipart `file_id` body | Token GET followed by POST with no body | Shared helper performs token GET then bodyless POST |
| Final propagation request | Reused the earlier token and posted multipart data | Acquires a token for the final call, then performs a bodyless POST | Shared helper acquires a fresh token and performs a bodyless POST |
| XSRF token header | `X-Goog-Colab-Token` | `X-Goog-Colab-Token` | `X-Goog-Colab-Token` |
| Query parameters | `authuser=0`, `authtype`, `version=2`, `dryrun`, `propagate=true`, `record=false` | Same values; `authuser=0` is added by the common request layer | Same values; `authuser=0` remains added by `_issue_request` |
| Request body | Multipart `file_id=empty.ipynb` | No request body | No request body |
| Token response schema | Loosely parsed dictionary | Required string `token` | `CredentialsPropagationToken` with `StrictStr` |
| Result response schema | Loosely parsed dictionary | Required boolean `success`; optional string redirect URI | `CredentialsPropagationResult` with `StrictBool` and optional `StrictStr` |
| XSSI handling | Manually split in automation code | Common response parsing | Existing client XSSI stripping before strict validation |
| Success decision | Final HTTP 200 alone | Valid response with `success === true` | Valid response with `success is True`; final `false` raises |
| Kernel success reply | Generated through the standard Jupyter message helper | Explicit Colab `input_reply` envelope | Explicit `build_colab_input_reply` envelope sent through `WSSession.send(kernel_socket, "stdin", reply)` |
| Failure reply | No reply on propagation HTTP failure | `content.value.error` is set | Sends a generic, redacted `content.value.error` |
| Client session ID source | Implicit helper state; request header could become the parent | Client-side Jupyter session used for outgoing messages | `wsclient.session.session`, the ID placed in outgoing `header.session` |
| `parent_header` | Could be replaced by the intercepted request header | `{}` | `{}` |
| `metadata` / channel | Helper defaults | `{}` / `stdin` | `{}` / `stdin` |
| HTTP errors | Printed status/body; command could still succeed | Throw | Sanitized `CredentialsPropagationError`; command exits nonzero |
| Business `success=false` | Final HTTP 200 could be reported as success | Throw on final propagation | Final propagation raises and cannot print the success marker |
| Remote kernel error | Printed traceback while `auth`/`drivemount` exited zero | Operation fails | `auth` and `drivemount` map it to a nonzero Typer exit |
| Sensitive logging | Full response body and full OAuth URL could enter logs | Does not require storing sensitive values | Redacts sensitive headers, omits response bodies, and stores only URL structure |

## Reply contract

The reply is deliberately constructed without `Session.msg()` so all
Colab-specific fields are explicit:

- `header.msg_type` is `input_reply`.
- `header.session` is the current Jupyter client's session identifier.
- `header.username` is `username` and `header.version` is `5.0`.
- `content.value.type` is `colab_reply`.
- `content.value.colab_msg_id` is copied unchanged from the intercepted
  request.
- `channel` is `stdin`; `metadata` and `parent_header` are empty objects.
- Failures add only the generic `content.value.error` string
  `Credentials propagation failed`.
- The complete envelope is sent through the websocket session with the kernel
  socket and `stdin` channel. No duplicate top-level `msg_id` or `msg_type`
  fields are added.

If the websocket session, kernel socket, client session ID, or Colab message
ID is unavailable or invalid, reply construction/sending fails closed and the
interactive CLI command exits nonzero.

## Security boundary

Diagnostics may record the operation, runtime endpoint, HTTP status, boolean
success, error category, message ID, and authorization URL scheme, hostname,
path, and query parameter names. They must not record credentials, cookies,
XSRF token values, response bodies, complete authorization URLs, or query
parameter values.

This comparison and the unit tests are static. They do not claim that a real
Drive mount is fixed; live validation remains a separately approved step.
