// Kakao login: stub, disabled until a Kakao app (REST key + redirect URI) is approved. Same flow as Google when enabled.
export async function onRequestGet({ request }) {
  const next = new URL(request.url).searchParams.get('next') || '/';
  return Response.redirect(new URL((next.startsWith('/en/') ? '/en/login/' : '/login/') + '?error=kakao_disabled', request.url).toString(), 302);
}
