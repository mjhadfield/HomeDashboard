-- Pin order replaces the plain "pinned" flag. Pinning assigns the next position in a sequence shared
-- across links AND projects (so the Launchpad's merged tile order matches the order things were actually
-- pinned in); unpinning clears it, and the next pin always takes a fresh, higher slot rather than
-- reusing a freed one — so an already-pinned item never moves just because a different one was unpinned.
-- NULL = not pinned.
ALTER TABLE links    ADD COLUMN pin_order INTEGER;
ALTER TABLE projects ADD COLUMN pin_order INTEGER;

-- Backfill: preserve today's effective order (links pinned-block first, then projects pinned-block, each
-- in their existing sort/name order) so nothing visibly reshuffles on upgrade.
UPDATE links SET pin_order = (
    SELECT rn FROM (SELECT id, ROW_NUMBER() OVER (ORDER BY sort, name) AS rn FROM links WHERE pinned = 1) sub
    WHERE sub.id = links.id
) WHERE pinned = 1;

UPDATE projects SET pin_order = (
    SELECT rn + (SELECT COUNT(*) FROM links WHERE pinned = 1)
    FROM (SELECT slug, ROW_NUMBER() OVER (ORDER BY sort, name) AS rn FROM projects WHERE pinned = 1) sub
    WHERE sub.slug = projects.slug
) WHERE pinned = 1;

ALTER TABLE links    DROP COLUMN pinned;
ALTER TABLE projects DROP COLUMN pinned;
