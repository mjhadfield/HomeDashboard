-- Which link (if any) the new SQL DB page embeds in its iframe (mirrors for_media/for_music above). At most one
-- at a time — enforced in application code, not a constraint, same as is_primary/for_media/for_music.
ALTER TABLE links ADD COLUMN for_sql INTEGER NOT NULL DEFAULT 0;
