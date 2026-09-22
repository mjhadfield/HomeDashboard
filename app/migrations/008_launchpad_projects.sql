-- A project can now also show up as its own tile in the Launchpad (the primary place for launching things),
-- alongside ordinary links — a web project's tile is a plain clickthrough link (it still start/stops from the
-- Projects card); a desktop project's tile *is* how you start it, since it has no page of its own to link to.
ALTER TABLE projects ADD COLUMN show_launchpad INTEGER NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN icon TEXT NOT NULL DEFAULT '';
