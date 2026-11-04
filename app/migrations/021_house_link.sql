-- Which link (if any) the House page embeds in its iframe (Home Improvements, ~/Projects/HomeImprovements). Mirrors
-- for_ai: at most one at a time, enforced in application code.
ALTER TABLE links ADD COLUMN for_house INTEGER NOT NULL DEFAULT 0;
