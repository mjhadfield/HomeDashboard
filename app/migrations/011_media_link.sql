-- Which link (if any) the Media page embeds in its iframe. At most one at a time — enforced in
-- application code, not a constraint, same as how is_primary already works.
ALTER TABLE links ADD COLUMN for_media INTEGER NOT NULL DEFAULT 0;
