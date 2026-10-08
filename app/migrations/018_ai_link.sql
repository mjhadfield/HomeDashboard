-- Which link (if any) the Odysseus page embeds in its iframe (mirrors for_media/for_music/for_sql). At most one
-- at a time — enforced in application code, not a constraint, same as is_primary/for_media/for_music/for_sql.
ALTER TABLE links ADD COLUMN for_ai INTEGER NOT NULL DEFAULT 0;
