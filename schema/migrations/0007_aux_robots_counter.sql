-- Auxiliary robots.txt dispatches have their own daily ceiling. They must not consume
-- content-request slots: one new host otherwise costs two of the 200/day budget.
ALTER TABLE web_daily_fetch ADD COLUMN robots INTEGER NOT NULL DEFAULT 0;
