-- Pinned links/projects sort to the top (and pinned links always show in the compact Launchpad).
ALTER TABLE links    ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
