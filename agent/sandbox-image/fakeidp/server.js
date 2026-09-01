"use strict";
/*
 * Fake OpenID Connect provider for ai-dev-workflow e2e.
 *
 * Serves the declared test users with Entra-shaped claims (oid / preferred_username / email / name
 * / roles) so a generated OIDC app can be sign-in tested against it -- no real tenant, no client
 * secret, no consent, no MFA. Started by agent/src/fake_idp.py inside the sandbox; the app's authority
 * config is overridden to point here.
 *
 *   node server.js --port <port> --config <path-to-users.json>
 *
 * users.json: { "issuerPath": "/aidw/v2.0", "clientId": "aidw-test-client",
 *               "clientSecret": "aidw-test-secret",
 *               "users": [{ "oid": "<guid>", "name": "...", "email": "...", "roles": ["Admin"] }] }
 *
 * Design notes (from the audit's spike checklist):
 * - issuer host is `localhost` (NOT 127.0.0.1): it must match the browsed BASE_URL origin, or the
 *   form_post callback is cross-site and the app's correlation cookie is dropped ("Correlation failed").
 * - access tokens are JWTs (resource server + accessTokenFormat 'jwt') carrying aud/scp/roles/oid,
 *   because generated .NET APIs validate bearer JWTs; opaque tokens would 401 every SPA->API call.
 * - conformIdTokenClaims:false so roles/oid survive in the ID token that web apps read.
 * - devInteractions replaced by a one-button-per-user login page (no passwords).
 * - redirectUriAllowed overridden to any localhost / 127.0.0.1 callback -- safe in-sandbox, and
 *   the app's exact callback path is unknown ahead of time.
 */

const fs = require("fs");
const { URL } = require("url");

function arg(name, def) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : def;
}

const PORT = parseInt(arg("port", "9400"), 10);
const CONFIG_PATH = arg("config", "");
const cfg = CONFIG_PATH ? JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8")) : {};
const ISSUER_PATH = cfg.issuerPath || "/aidw/v2.0";
const CLIENT_ID = cfg.clientId || "aidw-test-client";
const CLIENT_SECRET = cfg.clientSecret || "aidw-test-secret";
const USERS = Array.isArray(cfg.users) ? cfg.users : [];
const ISSUER = `http://localhost:${PORT}${ISSUER_PATH}`;

function userById(id) {
  return USERS.find((u) => u.oid === id) || USERS[0];
}
function claimsFor(u) {
  return {
    sub: u.oid,
    oid: u.oid,
    preferred_username: u.email || u.name,
    email: u.email || undefined,
    name: u.name,
    roles: u.roles || [],
  };
}

async function main() {
  const { default: Provider } = await import("oidc-provider");

  const provider = new Provider(ISSUER, {
    clients: [
      {
        client_id: CLIENT_ID,
        client_secret: CLIENT_SECRET,
        // Confidential (web app) AND public/PKCE (SPA) both work against one client: a secret is
        // present for client_secret_post, and PKCE is accepted regardless.
        token_endpoint_auth_method: "client_secret_post",
        grant_types: ["authorization_code", "refresh_token"],
        response_types: ["code"],
        // redirectUriAllowed (below) is what actually authorizes callbacks; this non-empty list
        // only satisfies the constructor's validation.
        redirect_uris: [`http://localhost:${PORT}/__unused_callback__`],
      },
    ],
    // Roles/oid are non-standard; keep them in the ID token web apps read.
    conformIdTokenClaims: false,
    pkce: { required: () => false }, // allow both PKCE and plain confidential-client flows
    scopes: ["openid", "profile", "email", "offline_access"],
    claims: {
      openid: ["sub"],
      profile: ["name", "preferred_username", "oid", "roles"],
      email: ["email"],
    },
    features: {
      // JWT access tokens with app-shaped claims so a .NET API's bearer-JWT validation passes.
      resourceIndicators: {
        enabled: true,
        defaultResource: () => `api://${CLIENT_ID}`,
        getResourceServerInfo: () => ({
          scope: "access_as_user",
          audience: `api://${CLIENT_ID}`,
          accessTokenFormat: "jwt",
        }),
      },
      devInteractions: { enabled: false },
    },
    // Put roles/oid into both tokens' payloads.
    extraTokenClaims: async (ctx, token) => {
      const u = userById(token.accountId);
      return u ? { oid: u.oid, roles: u.roles || [], preferred_username: u.email || u.name } : {};
    },
    async findAccount(_ctx, id) {
      const u = userById(id);
      if (!u) return undefined;
      return { accountId: id, claims: async () => claimsFor(u) };
    },
    async renderError(ctx, out, _err) {
      ctx.type = "html";
      ctx.body = `<h1>aidw fake IdP error</h1><pre>${JSON.stringify(out)}</pre>`;
    },
  });

  provider.proxy = true;

  // Accept any localhost/127.0.0.1 callback -- the generated app's exact path isn't known here.
  const { Client } = provider;
  Client.prototype.redirectUriAllowed = function redirectUriAllowed(uri) {
    try {
      const u = new URL(uri);
      return (u.protocol === "http:" || u.protocol === "https:") &&
        (u.hostname === "localhost" || u.hostname === "127.0.0.1");
    } catch {
      return false;
    }
  };

  // oidc-provider serves its routes at ITS OWN root (/.well-known/..., /auth, /token). The issuer
  // carries a path (/aidw/v2.0) so Microsoft.Identity.Web composes discovery there, so this server
  // is MOUNTED under that prefix: strip it from every request before delegating, and the provider's
  // absolute URLs (which include the prefix) map straight back. Interaction routes are handled here.
  const interactionUrl = /^\/interaction\/([^/]+)$/;
  const submitUrl = /^\/interaction\/([^/]+)\/login$/;

  const rawHandler = provider.callback();
  const server = require("http").createServer(async (req, res) => {
    // Map external /aidw/v2.0/* to the provider's internal /* (default "/" for the bare prefix).
    let internal = req.url;
    if (internal === ISSUER_PATH) internal = "/";
    else if (internal.startsWith(ISSUER_PATH + "/")) internal = internal.slice(ISSUER_PATH.length);
    req.url = internal;

    const path = internal.split("?")[0];
    const showMatch = path.match(interactionUrl);
    const postMatch = path.match(submitUrl);
    try {
      if (showMatch && req.method === "GET") {
        const uid = showMatch[1];
        const buttons = USERS.map(
          (u) =>
            `<form method="post" action="${ISSUER_PATH}/interaction/${uid}/login" style="margin:6px 0">
               <input type="hidden" name="oid" value="${u.oid}"/>
               <button type="submit" data-testid="login-${u.email || u.name}" style="padding:8px 16px;font-size:16px">
                 Sign in as ${u.name}${u.roles && u.roles.length ? ` (${u.roles.join(", ")})` : ""}
               </button>
             </form>`,
        ).join("\n");
        res.setHeader("content-type", "text/html");
        res.end(`<!doctype html><html><body><h2>aidw test sign-in</h2>${buttons}</body></html>`);
        return;
      }
      if (postMatch && req.method === "POST") {
        let body = "";
        for await (const chunk of req) body += chunk;
        const oid = new URLSearchParams(body).get("oid");
        const result = { login: { accountId: oid } };
        await provider.interactionFinished(req, res, result, { mergeWithLastSubmission: false });
        return;
      }
    } catch (e) {
      res.statusCode = 500;
      res.end(String(e));
      return;
    }
    rawHandler(req, res);
  });

  server.listen(PORT, () => {
    // eslint-disable-next-line no-console
    console.log(`aidw fake IdP listening on ${ISSUER} with ${USERS.length} user(s)`);
  });
}

main().catch((e) => {
  // eslint-disable-next-line no-console
  console.error(e);
  process.exit(1);
});
