// Kitchen Genome auth config. Supabase project: kitchengenome (hwlckcovkvoqlglvmfwy, ap-northeast-2).
// The anon key is public by design (RLS protects data). Never put the service_role key here.
window.KG_AUTH = {
  SUPABASE_URL: "https://hwlckcovkvoqlglvmfwy.supabase.co",
  SUPABASE_ANON_KEY: "sb_publishable_WYxWB97dFnWMPNoeS9AMOQ_Hjxj6X-Z", // publishable key (public; RLS protects data)
  PROVIDERS: ["google", "kakao"], // Naver intentionally not supported
  NEWSLETTER_DEFAULT_CHECKED: true, // 준태님 decision 2026-10-22
  CONSENT_VERSION: "2026-10-22"
};
