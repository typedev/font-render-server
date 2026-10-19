# Releasing and the signing key

Releases are built and published by this repository, `typedev/font-render-server`.

Installed servers update themselves from the public repository's GitHub Releases. Before
installing an update a server checks that `SHA256SUMS` of the release is signed by a key
it trusts; the archive's SHA256 must then match that file. Access to GitHub alone is
therefore not enough to push code onto installed servers: the private signing key is
needed too.

- **Private signing key** — 32 random bytes (Ed25519 seed), base64. Exists in two places
  only: the secret `RELEASE_SIGNING_KEY` of the `release` environment of the **public**
  repository, and the maintainer's password manager (backup). Never in a repository, never
  in `.env`, never in chat.
- **Public key** — listed in `TRUSTED_KEYS` in `src/font_render/release_key.py`, shipped
  with every server. Not secret.
- **Tool** — `scripts/release_key.py` (`generate`, `public`, `sign`, `verify`); it needs
  `cryptography`, run it with `uv run --with cryptography`.

## 1. Create the key (once)

On your own machine, in the repository root:

```bash
uv run --with cryptography scripts/release_key.py generate --out ~/release-signing-key.txt
# private key written to …/release-signing-key.txt (keep it secret, see docs/releasing.md)
# public key: 3q2+7w…=
# key id:     4fdea217d5c9d919
```

The file is created with mode 600 and the tool refuses to overwrite an existing one.

## 2. Back it up

Copy the file's single line into your password manager as a secure note, e.g. "Font Render
release signing key", with the key id. This is the only copy outside GitHub: GitHub
secrets cannot be read back, so without the backup a lost key means a key rotation (§6).

## 3. Create the `release` environment (public repository)

Already done for `typedev/font-render-server` (2026-10-18); for another repository:

`typedev/font-render-server` → **Settings → Environments → New environment** → `release`:

- **Required reviewers**: add yourself (`typedev`). Leave **Prevent self-review** off —
  with one maintainer you approve your own releases.
- **Deployment branches and tags**: *Selected branches and tags* → **Add deployment branch
  or tag rule** → Ref type **Tag**, pattern `v*`. A workflow run from a branch or a pull
  request can then not use the key.

Make sure two-factor authentication is on for the account: whoever logs in as you can
approve a release.

## 4. Store the private key as the environment secret

In the same environment: **Environment secrets → Add environment secret**, name
`RELEASE_SIGNING_KEY`, value = the file's line. Or with the GitHub CLI (the key goes from
the file straight to GitHub, it is not shown):

```bash
gh secret set RELEASE_SIGNING_KEY --env release --repo typedev/font-render-server \
    < ~/release-signing-key.txt
```

Then delete the local file (the password manager has it):

```bash
shred -u ~/release-signing-key.txt    # macOS: rm -P
```

## 4b. PyPI (once, for the client package)

The release workflow publishes `font-render-client` to PyPI by **trusted publishing**: PyPI
trusts this repository's workflow, no API token is stored anywhere. On
[pypi.org](https://pypi.org) (log in, 2FA on) → **Your projects → Publishing → Add a new
pending publisher** → GitHub:

| Field | Value |
|---|---|
| PyPI project name | `font-render-client` |
| Owner | `typedev` |
| Repository name | `font-render-server` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

The `pypi` environment of `typedev/font-render-server` exists (tags `v*` only). The first
release creates the project on PyPI; after that the pending publisher becomes a normal one.

## 5. Publish the public key

Put the printed public key into `src/font_render/release_key.py`, commit, push:

```python
TRUSTED_KEYS = [
    "pZ324jLYI6ukn2E+oejxs6kudovbkuJmhahcpNAF9Ck=",  # 4fdea217d5c9d919, created 2026-10-18
]
```

Done for the current key (`4fdea217d5c9d919`, 2026-10-18; its private half is the `release`
environment secret). A server only installs releases signed by a key in the list it ships with.

## Making a release

1. Bump `version` in `pyproject.toml` **and** `client/pyproject.toml` (the build checks they
   match; `RENDER_VERSION` in `config.py` if pixels change), update `CHANGELOG.md`, commit to
   `main`.
2. Check pixels on the maintainers' server (lurkbox) with the release candidate as a dev build:
   `font-render --deploy` → `pixel-check compare --server` against the baseline.
3. Tag and push: `git tag v1.3.0 && git push origin v1.3.0` (`v1.3.0b1` →
   pre-release, beta channel).
4. The release workflow tests, builds and smoke-tests the install on Windows (about 10–15
   minutes), then waits at the **sign** job: `typedev/font-render-server` → **Actions** → the
   run → **Review deployments** → `release` → **Approve and deploy**. Check that the run is for
   the tag you just pushed before approving.
5. The workflow signs `SHA256SUMS`, publishes the GitHub Release, then `font-render-client`
   on PyPI. Installed servers see the release at their next update check (web admin →
   Maintenance → Check for updates shows it at once).
6. Update lurkbox (`font-render --update`) and run `pixel-check compare --server`.

Check any release by hand:

```bash
gh release download v1.3.0 --repo typedev/font-render-server -p 'SHA256SUMS*' -D /tmp/r
uv run --with cryptography scripts/release_key.py verify /tmp/r/SHA256SUMS
# OK: /tmp/r/SHA256SUMS signed by key 4fdea217d5c9d919
```

Signing locally (if CI cannot): `RELEASE_SIGNING_KEY=… uv run --with cryptography
scripts/release_key.py sign SHA256SUMS`, then upload `SHA256SUMS.sig` to the release.

## 6. Rotating the key

Planned rotation (e.g. every few years, or when moving the key elsewhere):

1. Generate a new key (§1), back it up (§2).
2. Add its public key to `TRUSTED_KEYS` **next to** the old one; release that version as
   usual — still signed with the old key, so servers accept it and learn the new key.
3. Replace `RELEASE_SIGNING_KEY` in the environment with the new key.
4. Once installs have updated (the web admin of each shows its version), remove the old
   public key from `TRUSTED_KEYS` in a later release.

## 7. If the key leaks

Someone with the key **and** the ability to publish a release on this repository can ship
code to installed servers. Act at once:

1. Delete `RELEASE_SIGNING_KEY` from the environment; check the public repository's
   releases, tags and Actions runs for anything you did not make, and delete it.
2. Generate a new key; release a version whose `TRUSTED_KEYS` contains **only** the new key,
   signed with the **old** key (the last time it is used) so that servers accept it.
3. Store the new key as the secret.

Servers that updated in step 2 no longer trust the old key. A server that has not updated
yet can still be fooled by a release signed with the old key, so after a leak ask studio
admins to update now (or reinstall with `install.ps1`, which fetches the newest release).

If the key is merely lost (no leak): restore it from the password manager; if that is gone
too, servers cannot verify new releases — publish the new public key and ask admins to
reinstall with `install.ps1` (their data is kept).
