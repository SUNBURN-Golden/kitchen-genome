-- Ownership of identities created while delivering an approved review.
-- Public support and withdrawal receipts travel with the published seed.
CREATE TABLE ai_promotion_identity (
  kind TEXT NOT NULL CHECK (kind IN ('chef','branch','restaurant','alias')),
  entity_id TEXT NOT NULL,
  PRIMARY KEY (kind, entity_id)
);

-- Allow withdrawal of AI-owned identities. Foreign keys still protect entities
-- that have since acquired independent facts.
DROP TRIGGER trg_chef_no_delete;
CREATE TRIGGER trg_chef_no_delete BEFORE DELETE ON chef
WHEN NOT EXISTS (SELECT 1 FROM ai_promotion_identity WHERE kind='chef' AND entity_id=OLD.chef_id)
BEGIN SELECT RAISE(ABORT, 'append-only: chef rows are never deleted'); END;

DROP TRIGGER trg_restaurant_no_delete;
CREATE TRIGGER trg_restaurant_no_delete BEFORE DELETE ON restaurant
WHEN NOT EXISTS (SELECT 1 FROM ai_promotion_identity WHERE kind='restaurant' AND entity_id=OLD.restaurant_id)
BEGIN SELECT RAISE(ABORT, 'append-only: restaurant rows are never deleted'); END;

DROP TRIGGER trg_branch_no_delete;
CREATE TRIGGER trg_branch_no_delete BEFORE DELETE ON branch
WHEN NOT EXISTS (SELECT 1 FROM ai_promotion_identity WHERE kind='branch' AND entity_id=OLD.branch_id)
BEGIN SELECT RAISE(ABORT, 'append-only: branch rows are never deleted'); END;

DROP TRIGGER trg_alias_no_delete;
CREATE TRIGGER trg_alias_no_delete BEFORE DELETE ON alias
WHEN NOT EXISTS (SELECT 1 FROM ai_promotion_identity WHERE kind='alias' AND entity_id=OLD.alias_id)
BEGIN SELECT RAISE(ABORT, 'append-only: alias rows are never deleted'); END;
