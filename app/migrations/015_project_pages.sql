-- The Companion and SQL DB pages can take their address from a project (Music Companion, Music DB Explorer) as
-- well as from a link, so those apps don't have to be listed as Launchpad links just to be embedded. At most one
-- source per page, across links and projects together — enforced in application code (api_owner._apply_page).
ALTER TABLE projects ADD COLUMN for_music INTEGER NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN for_sql INTEGER NOT NULL DEFAULT 0;
