// Shared helpers for Kitchen Genome auth (Cloudflare Pages Functions + D1). No third-party deps.
const enc = new TextEncoder();
const b64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const fromB64u = (s) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));
export const SESSION_COOKIE = 'kg_session';
export const SESSION_DAYS = 30;
export function randomToken(n = 32) { const a = new Uint8Array(n); crypto.getRandomValues(a); return b64u(a); }
export async function sha256b64u(s) { return b64u(await crypto.subtle.digest('SHA-256', enc.encode(s))); }
async function hmacKey(secret) { return crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']); }
// Signed value: base64url(json).base64url(hmac)
export async function sign(obj, secret) {
  const body = b64u(enc.encode(JSON.stringify(obj)));
  const sig = await crypto.subtle.sign('HMAC', await hmacKey(secret), enc.encode(body));
  return body + '.' + b64u(sig);
}
export async function verify(token, secret) {
  if (!token || !secret) return null;
  const [body, sig] = token.split('.');
  if (!body || !sig) return null;
  let ok = false;
  try { ok = await crypto.subtle.verify('HMAC', await hmacKey(secret), fromB64u(sig), enc.encode(body)); } catch { return null; }
  if (!ok) return null;
  try { const o = JSON.parse(new TextDecoder().decode(fromB64u(body))); if (o.exp && Date.now() / 1000 > o.exp) return null; return o; } catch { return null; }
}
export function getCookie(request, name) {
  const h = request.headers.get('Cookie') || '';
  for (const p of h.split(/;\s*/)) { const i = p.indexOf('='); if (i > 0 && p.slice(0, i) === name) return decodeURIComponent(p.slice(i + 1)); }
  return null;
}
export function cookie(name, value, { maxAge, path = '/' } = {}) {
  let c = `${name}=${encodeURIComponent(value)}; Path=${path}; HttpOnly; Secure; SameSite=Lax`;
  if (maxAge !== undefined) c += `; Max-Age=${maxAge}`;
  return c;
}
export function safeNext(n, fallback = '/') { return typeof n === 'string' && n.startsWith('/') && !n.startsWith('//') && !n.startsWith('/\\') ? n : fallback; }
export function redirect(location, cookies = []) {
  const h = new Headers({ Location: location, 'Cache-Control': 'no-store' });
  for (const c of cookies) h.append('Set-Cookie', c);
  return new Response(null, { status: 302, headers: h });
}
export function googleConfigured(env) { return !!(env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET && env.SESSION_SECRET && env.DB); }
export async function currentUser(request, env) {
  const s = await verify(getCookie(request, SESSION_COOKIE), env.SESSION_SECRET);
  if (!s || !env.DB) return null;
  return env.DB.prepare('SELECT id, provider, email, newsletter_opt_in FROM users WHERE id = ? AND withdrawn_at IS NULL').bind(s.uid).first();
}
