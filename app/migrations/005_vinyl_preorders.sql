-- The vinyl radar now watches label/shop pre-orders. The old MusicBrainz/Discogs "historic pressings" it collected
-- were the wrong thing (nothing upcoming), so they go. Everything else — pins, dismissals — is untouched.
DELETE FROM feed_items WHERE kind = 'vinyl' AND source IN ('musicbrainz', 'discogs');
DELETE FROM widget_cache WHERE widget = 'vinyl';
