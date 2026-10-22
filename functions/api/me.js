// GET /api/me -> { config: { google, kakao }, user: { email, newsletter_opt_in } | null }
import { currentUser, googleConfigured } from '../_lib/session.js';
export async function onRequestGet({ request, env }) {
  let user = null;
  try { user = await currentUser(request, env); } catch { user = null; }
  return Response.json({
    config: { google: googleConfigured(env), kakao: false },
    user: user ? { email: user.email, provider: user.provider, newsletter_opt_in: !!user.newsletter_opt_in } : null,
  }, { headers: { 'Cache-Control': 'no-store' } });
}
