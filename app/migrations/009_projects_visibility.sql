-- A project's two homes (the Projects card, for start/stop; the Launchpad, for launching) are now independent.
-- Default true, so every existing project keeps showing in Projects exactly as before this migration.
ALTER TABLE projects ADD COLUMN show_projects INTEGER NOT NULL DEFAULT 1;
