# Browser authentication (B4)

Implementation in progress on `feat/browser-auth`.

The gateway is a separate process and data volume, shipped in the Pi image.
It owns a single owner's password and server-side sessions. Pi receives only a
runtime credential; it never receives the gateway's ToolGate owner credential,
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
