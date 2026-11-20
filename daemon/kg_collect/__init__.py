"""kg_collect — the ACTIVE collection bot (discover -> evaluate -> fetch -> extract/label -> gaps -> schedule).
Sits on top of kg_daemon (ontology DB, claims, checkpointed runner). Layers: raw (outside git) -> extraction -> labels -> service data."""
FETCHER_VERSION = 'fetch/1.1'
EXTRACTOR_VERSION = 'rules/1.9'
