import { cookie, redirect, safeNext, SESSION_COOKIE } from '../_lib/session.js';
// POST (from the header link via fetch) or GET fallback. Clears the session cookie.
const out = (request) => redirect(safeNext(new URL(request.url).searchParams.get('next'), '/'), [cookie(SESSION_COOKIE, '', { maxAge: 0 })]);
export const onRequestPost = ({ request }) => out(request);
export const onRequestGet = ({ request }) => out(request);
