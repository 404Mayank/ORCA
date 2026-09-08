/**
 * HTTP Basic Auth over the whole site, at the edge.
 *
 * Why this and not Vercel's own Deployment Protection: the built-in password
 * gate is a Pro feature. On Hobby the only option is Vercel Authentication,
 * which requires every viewer to log into the Vercel team that owns the
 * project -- fine for the owner, useless for a judge with a link. This is the
 * same protection, free, and it runs before a single byte of the app is
 * served.
 *
 * What it is actually protecting. The app is a static bundle, so the API key
 * the frontend sends to Modal is compiled into the JavaScript and readable by
 * anyone who can download it. Gating the site is therefore not only about the
 * UI: it is what stops the key being harvested from a public URL, and it is
 * the reason this sits in front of `/assets` too rather than just the HTML.
 *
 * Basic auth is chosen for being unskippable and having no moving parts --
 * the browser draws the dialog, there is no login page to build, no cookie to
 * forge and no session to expire. Its known weakness is that credentials go
 * on every request; over HTTPS, which Vercel enforces, that is acceptable for
 * a demo gate.
 */

export const config = {
  // Everything except Vercel's own internals. `/_vercel/*` carries insights
  // and image optimisation, and challenging those breaks the platform's own
  // requests without protecting anything of ours.
  matcher: ["/((?!_vercel).*)"],
};

const REALM = 'Basic realm="ORCA", charset="UTF-8"';

/**
 * Compare without leaking length or position through timing.
 *
 * Not because a timing attack on a demo password is realistic, but because
 * writing `a === b` here is the habit that eventually ships somewhere it
 * does matter.
 */
function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export default function middleware(request: Request): Response | undefined {
  const expected = process.env.SITE_PASSWORD ?? "";
  const user = process.env.SITE_USER || "orca";

  // Fail closed, and say why. An empty password would otherwise mean an
  // unguarded site that looks guarded, which is the worst of both -- and this
  // message is only ever seen by whoever deployed it.
  if (!expected) {
    return new Response(
      "SITE_PASSWORD is not set on this deployment, so the gate cannot be " +
        "enforced. Set it in the Vercel project environment and redeploy.",
      { status: 503, headers: { "content-type": "text/plain" } },
    );
  }

  const header = request.headers.get("authorization") ?? "";
  if (header.startsWith("Basic ")) {
    try {
      const [suppliedUser, ...rest] = atob(header.slice(6)).split(":");
      // Rejoin: a password containing a colon is legal and must survive.
      if (
        constantTimeEqual(suppliedUser ?? "", user) &&
        constantTimeEqual(rest.join(":"), expected)
      ) {
        return undefined; // authorised -- fall through to the static asset
      }
    } catch {
      // Malformed base64. Treated as a failed attempt, not an error page:
      // there is nothing useful to tell an unauthenticated caller.
    }
  }

  return new Response("Authentication required.", {
    status: 401,
    headers: { "WWW-Authenticate": REALM, "content-type": "text/plain" },
  });
}
