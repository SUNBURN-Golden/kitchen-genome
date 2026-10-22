// GET /auth/google/callback -> verify state, exchange code (PKCE), upsert user in D1, set signed session cookie.
import { verify, getCookie, cookie, redirect, sign, randomToken, SESSION_COOKIE, SESSION_DAYS, googleConfigured } from '../../_lib/session.js';
function fromB64u(s) { return new TextDecoder().decode(Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4)), (c) => c.charCodeAt(0))); }
export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  const clear = cookie('kg_oauth', '', { maxAge: 0, path: '/auth/' });
  if (!googleConfigured(env)) return redirect('/login/?error=not_configured', [clear]);
  const tx = await verify(getCookie(request, 'kg_oauth'), env.SESSION_SECRET);
  const fail = (code) => redirect(((tx && tx.lang === 'en') ? '/en/login/' : '/login/') + '?error=' + code, [clear]);
  if (!tx || !url.searchParams.get('state') || url.searchParams.get('state') !== tx.state) return fail('state');
  if (url.searchParams.get('error') || !url.searchParams.get('code')) return fail('denied');
  const r = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ code: url.searchParams.get('code'), client_id: env.GOOGLE_CLIENT_ID, client_secret: env.GOOGLE_CLIENT_SECRET,
      redirect_uri: `${url.origin}/auth/google/callback`, grant_type: 'authorization_code', code_verifier: tx.verifier }),
  });
  if (!r.ok) return fail('token');
  const tok = await r.json();
  let claims;
  try { claims = JSON.parse(fromB64u(tok.id_token.split('.')[1])); } catch { return fail('token'); }
  // id_token came straight from Google's token endpoint over TLS; still check audience/issuer/expiry.
  if (claims.aud !== env.GOOGLE_CLIENT_ID || !['https://accounts.google.com', 'accounts.google.com'].includes(claims.iss) || claims.exp < Date.now() / 1000) return fail('token');
  if (!claims.sub || !claims.email || claims.email_verified === false) return fail('email');
  const now = new Date().toISOString();
  let user = await env.DB.prepare('SELECT id, withdrawn_at FROM users WHERE provider = ? AND provider_sub = ?').bind('google', claims.sub).first();
  if (!user) {
    const id = randomToken(16);
    await env.DB.prepare('INSERT INTO users (id, provider, provider_sub, email, newsletter_opt_in, consent_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)')
      .bind(id, 'google', claims.sub, claims.email, tx.news ? 1 : 0, tx.news ? now : null, now).run();
    user = { id };
  } else {
    // Returning user: refresh email; re-signup after withdrawal reactivates with the new consent choice.
    if (user.withdrawn_at) await env.DB.prepare('UPDATE users SET email = ?, withdrawn_at = NULL, newsletter_opt_in = ?, consent_at = ? WHERE id = ?').bind(claims.email, tx.news ? 1 : 0, tx.news ? now : null, user.id).run();
    else await env.DB.prepare('UPDATE users SET email = ? WHERE id = ?').bind(claims.email, user.id).run();
  }
  const sess = await sign({ uid: user.id, exp: Math.floor(Date.now() / 1000) + SESSION_DAYS * 86400 }, env.SESSION_SECRET);
  return redirect(tx.next || '/', [clear, cookie(SESSION_COOKIE, sess, { maxAge: SESSION_DAYS * 86400 })]);
}
