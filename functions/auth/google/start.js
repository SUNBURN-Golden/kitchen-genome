// GET /auth/google/start?news=1|0&next=/path  -> Google OAuth (openid email) with state cookie + PKCE.
import { randomToken, sha256b64u, sign, cookie, safeNext, redirect, googleConfigured } from '../../_lib/session.js';
export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  const next = safeNext(url.searchParams.get('next'), '/');
  const en = next.startsWith('/en/');
  if (!googleConfigured(env)) return redirect((en ? '/en/login/' : '/login/') + '?error=not_configured');
  const state = randomToken(24), verifier = randomToken(48);
  const news = url.searchParams.get('news') !== '0'; // pre-checked by default
  const tx = await sign({ state, verifier, news, next, lang: en ? 'en' : 'ko', exp: Math.floor(Date.now() / 1000) + 600 }, env.SESSION_SECRET);
  const p = new URLSearchParams({
    client_id: env.GOOGLE_CLIENT_ID, redirect_uri: `${url.origin}/auth/google/callback`, response_type: 'code',
    scope: 'openid email', state, code_challenge: await sha256b64u(verifier), code_challenge_method: 'S256', prompt: 'select_account',
  });
  return redirect('https://accounts.google.com/o/oauth2/v2/auth?' + p, [cookie('kg_oauth', tx, { maxAge: 600, path: '/auth/' })]);
}
