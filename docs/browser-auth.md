# Browser authentication (B4)

Backend implemented on `feat/browser-auth`; release and ToolGate integration gates
are listed below. No dashboard screens are included.

The gateway is a separate process and data volume, shipped in the Pi image.
It owns a single owner's password and server-side sessions. Pi receives only a
hash of the runtime credential; it never receives the gateway's ToolGate owner credential,
password hash, session database or TLS private key. No service credential reaches
the browser. Only explicitly listed runtime operations may be forwarded.

The cookie is Secure, HttpOnly, SameSite=Strict and scoped to `/`. Unsafe requests
require an exact allowed Origin and a session-bound CSRF token, including login.
Successful login rotates the session. Logout, expiry, password reset and explicit
revocation invalidate server-side sessions. Password recovery requires access to
the host, never an email link or a service execution credential.

ToolGate integration contract requested from its owner: `X-ToolGate-Owner-Key`
on `GET /v2/owner/requests` and `POST /v2/owner/requests/{id}/decision`.
No fallback to ToolGate's admin or execution channel is permitted.

Recovery restores the password verifier, but invalidates every restored session.
A host password reset grants a new login; it cannot reconstruct lost transcripts,
vault keys or deleted memories, and cannot recall an action already dispatched.

## Backend contract

| Operation | Request / result |
| --- | --- |
| `GET /auth/session` | Creates an anonymous cookie if necessary; returns `authenticated`, `csrf_token`, `session_id`, absolute `expires_at`, `setup_required`. |
| `POST /auth/login` | JSON `{"password":"..."}`; rotates the cookie and CSRF token on success. |
| `POST /auth/logout` | Revokes the current session and clears its cookie. |
| `GET /auth/sessions` | Lists session IDs and timestamps, never bearer tokens. |
| `POST /auth/sessions/{id}/revoke` | Revokes one session. |
| `POST /auth/revoke-all` | Revokes every session, including the caller's. |
| `/api/pi/{path}` | Only methods and paths in `pi/browser_contract.py`; forwards using `X-Pi-Gateway-Key`. |
| `GET /api/owner/requests` | Reads the approval list through the dedicated ToolGate owner channel. |
| `POST /api/owner/requests/{id}/decision` | JSON `status` (`approved`, `rejected`, `dismissed`) and optional `note`; uses only the owner channel. |
| `GET /health` | Reports login setup and dependency health without returning conversation or approval content. |

For every unsafe request, including login, send the exact configured `Origin`,
`X-CSRF-Token` from `/auth/session`, the cookie, and `Content-Type: application/json`
when a JSON body is required. After login use the **new** CSRF token. The frontend
must handle 401 by showing sign-in, 403 by reloading request verification state,
429 by waiting, and 502/503 as explicit dependency failures. Memory notices pass
through unchanged; they must not depend on the model's reply.

Sessions expire after 30 minutes idle or 24 hours absolute, whichever comes first.
Anonymous login sessions expire after ten minutes. Attempts are limited durably to
five per source address per five minutes and thirty globally per fifteen minutes.
Forwarded IP headers are not trusted. A reverse proxy therefore shares the source
limit; rate limits are deliberately not an identity assertion. Revocation applies
to subsequent request admission; it cannot recall an already admitted operation.
No request is automatically retried after a service failure.

The password accepts 15–1024 characters, including spaces and Unicode. A random
16-byte salt and scrypt verifier (N=131072, r=8, p=1) are stored in SQLite;
password hashes run one at a time to bound memory. Session bearer tokens are random
256-bit values; only SHA-256 hashes are stored. Password reset and session rotation
are transactional. A reset racing a slow password check cannot mint a stale session.
Backups remain sensitive: they include the password verifier and service secrets.

## Running and recovery

`python -m gateway serve` runs HTTPS on port 8050 and requires `GATEWAY_ORIGIN`
(an exact HTTPS origin without a trailing slash) and a distinct `PI_GATEWAY_KEY`
of at least 32 characters. Optional service addresses are `GATEWAY_PI_URL`
and `GATEWAY_TOOLGATE_URL`; defaults are `http://pi:8050` and
`http://toolgate-api:8010`. `GATEWAY_TOOLGATE_OWNER_KEY` is issued by ToolGate,
never an admin or execution key. Without it, owner operations return 503.
`GATEWAY_DB_PATH` defaults to `/auth/auth.db`. Configuration comes from environment
variables; no configuration file or inherited proxy environment is read by the gateway.

The Pi worker gets `PI_GATEWAY_KEY_SHA256`, containing the SHA-256 hex digest of
the gateway runtime key. Its existing `PI_ADMIN_KEY` remains a host recovery
credential. Never give the worker the gateway's environment file or `/auth` volume.
The standalone Pi Compose file remains an authenticated development/recovery API;
the companion Compose file provides the browser deployment boundary.

On the host, `python -m gateway setup --db PATH` sets the initial password;
`reset-password` replaces it and revokes all browser sessions. Both prompt without
echo and explain what the password protects. There is no network setup or reset
endpoint, recovery email, or claim-by-first-visitor behavior. `revoke-all` can be
used without changing the password. In companion these are `./conker auth ...`.

The gateway creates a local TLS identity once, in the auth volume. Export its public
certificate with `certificate` and trust that certificate on the client before
login; do not disable certificate verification. It is valid for one year.
`renew-certificate` creates a new identity explicitly; restart the gateway and
trust the newly exported certificate. Missing, mismatched or expired TLS material
causes startup to fail. Renewal interrupted between file replacements fails closed;
rerun the host renewal command. This does not reset the password or recover data.

## Verification and remaining gates

Run `python -m pytest tests/test_gateway_*.py -q` and
`python scripts/auth_mutation_drill.py`. On shells without glob expansion, name
the four gateway test files explicitly. The live test starts real HTTPS and Pi
processes, saves a conversation, rejects a logged-out cookie, and verifies host
password recovery. Only terminal input is supplied by a test harness.

ToolGate's `/v2/owner/...` routes do **not yet exist in the inspected checkout**.
The forwarding contract has been tested against a service fixture, not presented
as a working owner approval round trip. Its implementation and live negative test
(an execution key cannot approve) remain a dependency owned by the ToolGate instance.
Publish a versioned Pi image containing this package before activating companion's
gateway deployment. No release tag or published-image claim is made by this branch.
