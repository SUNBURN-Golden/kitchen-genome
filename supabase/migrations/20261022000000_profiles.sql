-- Kitchen Genome: profiles keyed to auth.users, newsletter consent (boolean + timestamp).
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  provider text check (provider in ('google','kakao')),
  newsletter_opt_in boolean not null default false,
  newsletter_consent_at timestamptz,
  newsletter_withdrawn_at timestamptz,
  consent_version text,
  signup_lang text check (signup_lang in ('ko','en')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.profiles enable row level security;

create policy "profiles_select_own" on public.profiles for select to authenticated using ((select auth.uid()) = id);
create policy "profiles_insert_own" on public.profiles for insert to authenticated with check ((select auth.uid()) = id);
create policy "profiles_update_own" on public.profiles for update to authenticated using ((select auth.uid()) = id) with check ((select auth.uid()) = id);
-- No delete policy: account deletion cascades from auth.users.

-- Server-side guards: consent timestamp is set by the DB, not trusted from the client.
create or replace function public.profiles_consent_guard() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  new.updated_at := now();
  if tg_op = 'INSERT' then
    new.created_at := now();
    new.newsletter_consent_at := case when new.newsletter_opt_in then now() else null end;
    new.newsletter_withdrawn_at := null;
  elsif new.newsletter_opt_in is distinct from old.newsletter_opt_in then
    if new.newsletter_opt_in then new.newsletter_consent_at := now(); new.newsletter_withdrawn_at := null;
    else new.newsletter_withdrawn_at := now(); end if;
  else
    new.newsletter_consent_at := old.newsletter_consent_at;
    new.newsletter_withdrawn_at := old.newsletter_withdrawn_at;
    new.created_at := old.created_at;
  end if;
  return new;
end $$;
drop trigger if exists profiles_consent_guard on public.profiles;
create trigger profiles_consent_guard before insert or update on public.profiles
  for each row execute function public.profiles_consent_guard();
