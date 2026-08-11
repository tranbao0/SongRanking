# Demo registry access

A small Cloudflare Worker that gives anyone with a GitHub account
self-service, read-only access to the private registry data repo (see the
main README's "Git-synced registry database" section) - no email, no
manual per-person invite from the project owner, and unlike making the
data repo public, not crawlable/scrapable by anything that can't complete
a real GitHub login.

"Sign in with GitHub" on the page this Worker serves auto-invites the
visitor as a pull-only collaborator on the data repo, using a separate
owner-held credential (`GITHUB_INVITE_PAT`) to actually perform the
invite - the visitor's own OAuth token is only ever used to read back
their username, never to touch the repo.

## One-time setup

You'll go back and forth once between deploying and registering the
OAuth App, since each needs something from the other.

**1. Deploy once with placeholder secrets, to get the Worker's real URL:**

```bash
cd tools/demo-access
npm install
npx wrangler deploy
```

Note the `*.workers.dev` URL wrangler prints - you need it for step 2.

**2. Register a GitHub OAuth App:**

GitHub → Settings → Developer settings → OAuth Apps → New OAuth App.

- Homepage URL: anything (e.g. the main SongRanking repo's URL).
- Authorization callback URL: `https://<your-worker>.workers.dev/callback`

Save it, then note the **Client ID** and generate a **Client Secret**.

**3. Generate the invite-capable PAT:**

GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token.

- Repository access: **Only select repositories** → your registry data repo.
- Repository permissions → **Administration: Read and write** (this is what
  the "add a collaborator" API call requires - nothing else needs write
  access, and this token can't touch any other repository).

**4. Set the real secrets and edit `wrangler.toml`'s `[vars]` to your repo's owner/name:**

```bash
npx wrangler secret put GITHUB_CLIENT_ID
npx wrangler secret put GITHUB_CLIENT_SECRET
npx wrangler secret put GITHUB_INVITE_PAT
```

`wrangler.toml`'s `DATA_REPO_OWNER`/`DATA_REPO_NAME` aren't secret (they're
visible in the authorize/invite URLs anyway) - edit them directly, then:

```bash
npx wrangler deploy
```

Share the Worker's URL however you'd like people to find it - it's not
indexed or discoverable on its own, so pointing to it (a link in the main
README, a message, whatever) is what actually makes it "self-service"
rather than secret.

## Rotating credentials

Both the OAuth Client Secret and the invite PAT can be regenerated/revoked
independently, any time, without affecting anyone who already accepted an
invite - re-run the relevant `wrangler secret put` command with the new
value.
