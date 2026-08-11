/**
 * Self-service read-only access to the private registry data repo
 * (see SongRanking's README "Git-synced registry database" section).
 *
 * "Sign in with GitHub" -> the visitor is auto-invited as a pull-only
 * collaborator on DATA_REPO_OWNER/DATA_REPO_NAME. No email, no manual
 * review - GitHub's own OAuth login is the only gate, which is enough to
 * keep this off anonymous scrapers without needing the repo to be public.
 *
 * Two credentials are involved and must not be confused: the visitor's
 * own OAuth token (requested with no scopes - it's only ever used to look
 * up *their* username via GET /user, never to touch the repo) and
 * GITHUB_INVITE_PAT, a separate credential that belongs to the repo
 * owner and is what actually performs the invite. A visitor's token could
 * not grant themselves access even if they tried - inviting requires the
 * owner's own PAT, which never leaves this Worker.
 */

const GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize";
const GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token";
const GITHUB_API = "https://api.github.com";

// GitHub usernames are restricted to alphanumerics and hyphens (GitHub
// itself enforces this at account creation), so interpolating `login`
// into the HTML responses below can't inject markup - no separate
// escaping needed for that one value.

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/") return handleIndex(url, env);
    if (url.pathname === "/callback") return handleCallback(request, url, env);
    return new Response("Not found", { status: 404 });
  },
};

function handleIndex(url, env) {
  const state = crypto.randomUUID();
  const authorizeUrl = new URL(GITHUB_AUTHORIZE_URL);
  authorizeUrl.searchParams.set("client_id", env.GITHUB_CLIENT_ID);
  authorizeUrl.searchParams.set("redirect_uri", `${url.origin}/callback`);
  authorizeUrl.searchParams.set("state", state);
  authorizeUrl.searchParams.set("allow_signup", "true");

  const html = page(`
    <p>Sign in with GitHub to get read-only access to the registry data
    repo - a real, current snapshot of the catalogued channels, videos,
    and view-count history you can pull and try the system against,
    without building your own catalog from scratch.</p>
    <p><a class="button" href="${authorizeUrl.toString()}">Sign in with GitHub</a></p>
    <p>This only grants pull (read-only) access to one repository, nothing
    else on your GitHub account. You'll get a collaborator invite to
    accept afterward.</p>
  `);

  return new Response(html, {
    headers: {
      "content-type": "text/html; charset=utf-8",
      // HttpOnly + the callback comparing it against GitHub's returned
      // `state` is the CSRF defense here - a forged callback request
      // can't also forge this cookie. SameSite=Lax (not Strict) because
      // the browser arrives at /callback via a cross-site top-level
      // navigation (the redirect back from github.com), which Strict
      // would drop the cookie on.
      "set-cookie": `oauth_state=${state}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=600`,
    },
  });
}

async function handleCallback(request, url, env) {
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const cookieState = getCookie(request, "oauth_state");

  if (!code || !state || !cookieState || state !== cookieState) {
    return errorPage("This login link has expired or is invalid - go back and sign in again.");
  }

  const tokenResp = await fetch(GITHUB_TOKEN_URL, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({
      client_id: env.GITHUB_CLIENT_ID,
      client_secret: env.GITHUB_CLIENT_SECRET,
      code,
      redirect_uri: `${url.origin}/callback`,
    }),
  });
  const tokenData = await tokenResp.json();
  if (!tokenResp.ok || !tokenData.access_token) {
    return errorPage("GitHub sign-in failed - please go back and try again.");
  }

  const userResp = await fetch(`${GITHUB_API}/user`, {
    headers: {
      authorization: `Bearer ${tokenData.access_token}`,
      "user-agent": "songranking-demo-access",
      accept: "application/vnd.github+json",
    },
  });
  const userData = await userResp.json();
  if (!userResp.ok || !userData.login) {
    return errorPage("Couldn't read your GitHub username - please go back and try again.");
  }

  const inviteResp = await fetch(
    `${GITHUB_API}/repos/${env.DATA_REPO_OWNER}/${env.DATA_REPO_NAME}/collaborators/${userData.login}`,
    {
      method: "PUT",
      headers: {
        authorization: `Bearer ${env.GITHUB_INVITE_PAT}`,
        "user-agent": "songranking-demo-access",
        accept: "application/vnd.github+json",
        "content-type": "application/json",
      },
      body: JSON.stringify({ permission: "pull" }),
    }
  );

  if (!inviteResp.ok) {
    // Surface GitHub's own reason (e.g. "422 - cannot add the repository
    // owner as a collaborator") rather than a generic message - most
    // failures here are self-explanatory once you can see it, and hiding
    // it just turns every failure into a guessing game.
    const inviteData = await inviteResp.json().catch(() => ({}));
    const reason = inviteData.message || "no further detail from GitHub";
    return errorPage(
      `Couldn't grant access (GitHub returned ${inviteResp.status}: ${reason}).`
    );
  }

  return successPage(userData.login, env);
}

function getCookie(request, name) {
  const header = request.headers.get("cookie") || "";
  const match = header.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

function errorPage(message) {
  return new Response(page(`<p>${message}</p>`), {
    status: 400,
    headers: { "content-type": "text/html; charset=utf-8" },
  });
}

function successPage(login, env) {
  const body = `
    <p>Invite sent to <strong>${login}</strong> for
    <code>${env.DATA_REPO_OWNER}/${env.DATA_REPO_NAME}</code> (read-only).</p>
    <p>Check your GitHub notifications - or
    <a href="https://github.com/${env.DATA_REPO_OWNER}/${env.DATA_REPO_NAME}/invitations">this page</a>
    - to accept it, then:</p>
    <pre>git clone https://github.com/${env.DATA_REPO_OWNER}/${env.DATA_REPO_NAME}.git</pre>
  `;
  return new Response(page(body), { headers: { "content-type": "text/html; charset=utf-8" } });
}

function page(body) {
  return `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>SongRanking demo registry access</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 40em; margin: 3em auto; line-height: 1.5; padding: 0 1em; color: #1f2328; }
  .button { display: inline-block; padding: .6em 1.2em; background: #24292f; color: #fff; text-decoration: none; border-radius: 6px; }
  code, pre { background: #f6f8fa; border-radius: 4px; padding: .1em .4em; }
  pre { padding: .8em; overflow-x: auto; }
</style>
</head>
<body>
<h1>SongRanking demo registry</h1>
${body}
</body>
</html>`;
}
