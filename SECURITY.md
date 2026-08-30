# Security policy

## Reporting a vulnerability

**Please do not report security issues in a public issue.**

Pullbackup holds SSH private keys and has filesystem write access to your
backup destinations, so a vulnerability here is more consequential than in a
typical web app. Report privately so a fix can ship before details are public.

Use GitHub's private vulnerability reporting (**Security → Report a
vulnerability**), which opens a private security advisory visible only to the
maintainers.

Please include:

- what you can do with the issue, not just what looks wrong
- the version or commit you tested
- steps to reproduce, ideally against a throwaway instance
- whether it requires an authenticated session

You will get an acknowledgement within a few days. This is a small
self-hosted project maintained in spare time, so please allow reasonable time
for a fix before disclosing publicly.

## Supported versions

The latest release on the default branch is supported. There are no
long-term-support branches.

## Threat model

Understanding what Pullbackup does and does not defend against will tell you
whether something is a bug or expected behaviour.

**In scope**

- Bypassing authentication on any endpoint, whether via HTTP Basic or the
  login session cookie. This includes forging, tampering with, or replaying a
  session cookie, and using a cookie after sign-out, after it expires, or
  after the credential it was derived from changed.
- The login endpoint accepting a credential the middleware would reject, or
  answering anything other than 503 on an instance with no valid credential
  configured.
- Escaping the configured destination allowlist (`PULLBACKUP_DEST_ROOTS`,
  `PULLBACKUP_ZFS_DEST_ROOTS`) to read or write outside it — including via
  symlinks, races, or path traversal.
- Reading or exfiltrating the generated SSH private key through the
  application.
- Injecting arguments into the underlying `rsync`, `ssh`, or `syncoid`
  invocations from user-supplied task fields.
- Storing credentials in run logs, the API, or the UI.

**Out of scope**

- The instance being deployed without authentication configured. The app
  refuses to start without HTTP Basic credentials; removing that check
  yourself is your decision.
- Anything reachable only by an already-authenticated operator. An operator
  can legitimately configure tasks that run `rsync` against paths inside the
  allowlist — that is the product, not a vulnerability.
- Exposing the UI directly to the internet without a reverse proxy and TLS.
- Compromise of a source host you have configured, or of the machine running
  Pullbackup itself.
- The `aux_args` / `syncoid_extra_args` fields, which deliberately accept raw
  arguments. They are an authenticated-operator escape hatch by design.

## Hardening notes

- Give the SSH key the narrowest access that works on each source host —
  read-only, and restricted to the paths you actually pull.
- Keep the data volume (`/data`) off shared storage: it holds the database and
  the private key.
- Put the UI behind a reverse proxy that terminates TLS. The session cookie is
  only marked `Secure` when the request arrives over https, and HTTP Basic
  sends the credential on every single request.
- Sessions are single-user and held in process memory: there is no revocation
  list that survives a restart, and a restart signs everyone out. Sign-out
  revokes the session server-side, so a copied cookie stops working too.
- Set `PULLBACKUP_SESSION_SECRET` if you need to change the password without
  invalidating live sessions. Leaving it unset is the safer default: rotating
  the credential then also rotates every session.
