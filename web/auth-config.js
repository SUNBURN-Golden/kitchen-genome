// Kitchen Genome auth config. Fill in after the Supabase project exists.
// The anon key is public by design (RLS protects data). Never put the service_role key here.
window.KG_AUTH = {
  SUPABASE_URL: "",        // e.g. "https://<project-ref>.supabase.co"
  SUPABASE_ANON_KEY: "",   // Project Settings → API → anon public key
  PROVIDERS: ["google", "kakao"], // Naver intentionally not supported
  NEWSLETTER_DEFAULT_CHECKED: true, // 준태님 decision 2026-10-22
  CONSENT_VERSION: "2026-10-22"
};
