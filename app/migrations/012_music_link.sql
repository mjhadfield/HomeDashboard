-- Which link (if any) the Companion page embeds in its iframe (mirrors for_media above). At most one
-- at a time — enforced in application code, not a constraint, same as is_primary/for_media.
ALTER TABLE links ADD COLUMN for_music INTEGER NOT NULL DEFAULT 0;
