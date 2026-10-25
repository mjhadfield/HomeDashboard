# Citadel

A LAN home dashboard: one page for the household network — your projects (with start/stop), Jellyfin, bin
days, your calendar, booked gigs, new releases, a vinyl radar, gig radar and headlines for the artists you follow,
host vitals — plus a minimal guest page, a TV mode, and a Settings UI. Python (FastAPI) + vanilla
JS, no build step, SQLite for everything you configure.

Runs as a systemd **user** service. Designed to move to another machine by copying the folder.

## Install

```bash
cp .env.example .env        # fill in JELLYFIN_URL, BINS_UPRN, DIARY_ICAL_URL (first run only)
deploy/install.sh           # venv + deps + systemd user service + `hub` command
hub enrol                   # prints a one-time link (and QR) — open it on your own device
```

Then, once, on the host (these need sudo, so the installer only prints them):

```bash
sudo loginctl enable-linger "$USER"                                                       # start at boot, before login
sudo ufw allow from 192.168.0.0/24 to any port 8080 proto tcp comment 'Citadel'          # let the LAN in (if ufw is active)
```

Optional friendly name: `deploy/install.sh --mdns dash` publishes `dash.local` (needs `avahi-publish`).
Reserve the machine's IP in your router — it is on Wi-Fi — so bookmarks keep working.

## Navigation

The owner dashboard is several pages sharing one persistent shell — the rail (desktop, left) / tabbar (phone,
bottom) never disappears, switching pages never reloads the browser tab:

- **Home** — the card grid (Now, gigs, diary, projects, music, Launchpad, system…).
- **News** — Headlines, full-page: the same card as before, just given the whole page instead of a small tile,
  with its filters and search always visible.
- **Media** — an embedded Jellyfin (or whichever link is marked **Use as Media view** in Settings → Links),
  in an iframe that's created once and never reloaded — switching away and back keeps whatever was
  playing/browsing exactly where you left it.
- **Companion** / **SQL DB** — Music Companion and its database explorer, embedded the same way.
- **Odysseus** — the Odysseus AI workspace (`~/Projects/Odysseus`, port 7011), embedded the same way, from whichever
  link is marked **Use as Odysseus view**.
- **Music** — Spotify playlists built from your own listening, Discover, gig setlists, and full-page Releases and
  Vinyl radar. See [The Music page](#the-music-page).

Settings is still its own separate page (the gear icon) — everything else routes client-side via the URL hash
(`#home` / `#news` / `#media` / `#music` / `#sql` / `#listen` / `#ai`), so the back/forward buttons and reload both work normally.

## Who sees what

| Visitor | Gets |
|---|---|
| **Anyone on the LAN** (guest) | the guest page: only links you marked "show on guest page" (Jellyfin) |
| **Paired screen** (TV) | a read-only TV view showing only the panels you allowed |
| **Enrolled owner device** | the full dashboard, project start/stop, settings |

There are no passwords. A device becomes an owner by redeeming a **one-time, 15-minute link** printed by
`hub enrol` on the host, which sets a signed, long-lived cookie. `hub devices` lists devices; `hub revoke <id>`
removes one. Enrolling requires shell access to the host, which is the point.

Start/stop commands are stored as argument lists (no shell) and can only be run by an owner device.
Mutating requests also need an `X-Hub` header (CSRF guard) and pages carry a strict Content-Security-Policy.

A running project shows a live, ticking **uptime** (`7h 20m`, coarsening from seconds up to days as it grows) instead
of its health check's latency reading. It comes from systemd's own record of when the unit started — not the
dashboard's memory of starting it — so it's right even across a dashboard restart, or if you start a project's unit
yourself outside the UI.

## Settings

Everything is editable in the browser at **/settings** (owner devices only): links, projects (name, port, folder, start
command, environment), the bins and calendar widgets, TV panels, and devices. Inputs are validated on the server —
links must be http(s), commands become argument lists (no shell), environment names are checked.

## TV / screens

1. On the TV's browser open `http://<host>:8080/tv` — an unpaired screen shows a short code.
2. On an owner device: **Settings → Screens & devices → Pair a screen**, type the code.
3. The TV is now a read-only *screen* device and starts rotating its panels.

Choose which panels a screen may show under **Settings → Screens & devices**. The **diary is off by default** — a TV
is visible to guests. Panels you switch off are never sent to the screen at all (enforced server-side, not just hidden).
Pairing codes last 10 minutes and can only ever create screens, never owners.

## Launchpad and Projects

The **Launchpad** is the primary place for launching things — plain external links, and now projects too. A project
opts in under **Settings → Projects → Show in Launchpad**:

- A **web** project's tile is a plain clickthrough link to its address, with a live status dot — same as any other
  link. It still starts/stops from the **Projects** card as before.
- A **desktop** project has no page to link to, so its tile *is* the start button (a click starts it, same as
  Projects' own Start button — idempotent if it's already running).

A project can also turn off **Show in Projects** — for a standalone app with nothing to *stop* (a GUI you launch and
close yourself, like a desktop game), a row on the Projects card doesn't add anything. Turning it off only hides
the row; the project is exactly as startable as before, from its Launchpad tile. A web service you actually manage
(start, stop, watch its uptime) should keep **Show in Projects** on — that's what the card is for.

## Pinning

Pin any Launchpad tile or project (button on the tile/row, or in Settings) to keep it first. A pinned tile always
shows in the compact Launchpad, even one marked *expanded only*, so pinning is how you keep something visible once
the list overflows.

Pinning is positional, not a sort key: the first thing you pin takes slot 1, the next takes slot 2, and so on — a
link and a project pinned in sequence share the same ordering, so it works the same whichever you pin. An already-
pinned item keeps its slot until you unpin it; unpinning doesn't reshuffle anything else, and the next thing you pin
always takes a fresh slot at the end rather than filling the gap. The manual reorder arrows in Settings still work
within the pinned group if you want to rearrange them by hand.

**Adding a gig without leaving the dashboard.** Next gig's header has a small **+** next to Expand — it goes straight
to Settings with the "add a gig" form already open, rather than just the tab.

## Monitoring

The Monitoring tile shows live CPU/GPU stats — compact view splits CPU (left) and GPU (right), each as three
bar charts plus a big temperature reading at the bottom: CPU usage/busiest core/RAM, GPU usage/VRAM/power draw.
Expand it for everything (per-core usage, package + per-chiplet CPU temps, load averages, GPU clocks/fan, swap,
buffers+cached). GPU stats need `nvidia-smi` (NVIDIA only); the tile just omits GPU figures if it isn't there,
rather than erroring.

It's collected by a small background loop (`app/monitor.py`'s `Monitor`, started/stopped in `main.py`'s
lifespan — same pattern as `Health`/`Scheduler`, one instance per app run on `app.state.monitor`, not module
state) that reads sensors once a second, independent of any client. `GET /api/owner/monitoring` just returns
that latest reading — no sensor read happens inside the request, so it's near-instant. The browser polls it
directly every 2 seconds, deliberately outside the normal state/SSE refresh cycle: routing it through there
would mean every tick triggers a full dashboard refresh (including two `systemctl` calls) just to update one
tile. The frontend also keeps the tile's data in its own variable rather than on the main `state` object, since
`state` gets wholesale-replaced by every *unrelated* dashboard refresh (any widget's own refresh notifies over
SSE and triggers one — the System widget alone does this every 30s) — without that, the tile would flash back
to "Reading sensors…" on every one of those, even though its own data was perfectly current.

## Where things live

- `data/hub.sqlite` — projects, links, widget settings, cached widget data, devices (mode 0600). **Copy this to move.**
- `.env` — bootstrap values and server options (mode 0600, git-ignored). Widget values are read on the *first* run
  only; after that the database wins (change them in the app or with `hub seed`).
- `app/widgets/*.py` — one file per data source. Add a file exposing `widget = MyWidget()` and it is picked up.
- `web/` — the UI. `web/mock/` is the archived design mockup.

Schema upgrades live in `app/migrations/NNN_*.sql` and run at startup. Each script and its applied marker commit
together; a failed upgrade rolls back that script's schema and data changes so it can be corrected and retried.
Earlier successful migrations stay applied. The runner owns the transaction, so migration scripts must not contain
their own `BEGIN`, `COMMIT` or `ROLLBACK`, or operations that require running outside a transaction (such as `VACUUM`).

## Music & news cards

| Card | Source | Needs |
|---|---|---|
| **Next gig** | your booked tickets (Settings → Music & news) | nothing — countdown + a reminder the day before |
| **Releases** | MusicBrainz — **studio albums** by artists you follow, announced up to **12 months** ahead | nothing (key-free, 1 request/second). Singles, EPs, live albums, compilations and soundtracks are off by default |
| **Vinyl radar** | **pre-orders** from label/record-shop stores (Earache, Nuclear Blast, Century Media, Relapse, Deathwish, **Crash Records**, **Juno** by default) | nothing — stores are editable |
| **Gig radar** | Ticketmaster Discovery API, near your location — VIP / premium / hospitality packages left out, merch packages kept | a free API key (developer.ticketmaster.com) |
| **Headlines** | RSS/Atom feeds you choose, with include/exclude keyword rules | nothing (metal-leaning defaults included) |

**Order.** **Releases** is the one card that reads **oldest to newest**: albums from the last month first, then everything
announced, furthest away last. **Vinyl radar** and **Gig radar** read newest to oldest (by release / show date; a pre-order
with no release date goes by when it was listed). Anything you've **pinned** comes first in all of them. Each shows 10 rows
in its small card (scroll for the rest); the expanded view shows everything.

**Releases** looks back **one month** (Settings → Releases → *Look back*) and **12 months ahead** (*Look ahead*).
MusicBrainz — and iTunes, which I cross-checked across all 200 followed artists — only know about an album once it is
announced, so for most artists nothing is listed until a few weeks before: the window isn't the limit, the announcements are.
Placeholder dates years out are ignored. The query asks MusicBrainz for albums only, so an artist with a pile of singles
can't crowd an upcoming album out.

Each tile shows the album's artwork where the Cover Art Archive has it (only `coverartarchive.org`, plus the `archive.org`
storage nodes its images redirect through, are allowed by the page's CSP) — otherwise it falls back to a lettered tile like
Vinyl radar's, same as when a lookup fails or times out.

**Vinyl radar = things you can still pre-order.** MusicBrainz and Discogs catalogue pressings that *already exist*, so
they can't tell you about an upcoming one. Labels list a pressing the moment it goes on pre-order, and many stores run on
**Shopify**, which publishes its catalogue — including a pre-order collection — as public JSON. So each sync reads each
store's pre-order collection (one small request per store), keeps the vinyl by artists you follow, and merges the many
colour variants of one album into a single row. The same record at two stores shows once, from the store listed first.

Each tile shows the artwork (from the store's image CDN — only `cdn.shopify.com` and `imagescdn.juno.co.uk` are allowed by
the page's CSP), the artist and album on their own lines (long names wrap rather than being cut), then format, variants
and price, store, and a small right-aligned date (`DD/MM/YYYY`: **releases** where the store gives one, otherwise
**listed**). The expanded view lists every edition with its price, each linking to its store page.

- Prices are in each store's own currency (Earache and Juno are GBP; Nuclear Blast EUR; the US labels USD — mind import costs).
- **Release dates** appear where a store exposes one (Earache, Deathwish, Juno and Crash Records do).
  Newly listed pre-orders after the first sync are flagged **New**.
- A pre-order that ships or sells out disappears on the next sync (pinned ones stay).
- **Add any Shopify store** in Settings → Music & news: its web address plus the *collection* that lists pre-orders
  (the last part of `…/collections/pre-orders`). **Check** shows what a store offers before you rely on it.
- **Juno** isn't Shopify, so it is read from its public pre-order listing pages, by genre section (`metal`, `rock`,
  `punk-hardcore` by default; any of Juno's genre names work). That is ~100 page requests, so it is done **slowly and
  politely**: only pages its robots.txt allows, spaced by the Crawl-delay it asks for (5 s), and **at most once a day** (or
  when you change its sections). The first read takes about ten minutes in the background; the other stores are
  unaffected.
- **Can't be read:** Hive and EMP (both behind bot-detection/heavily client-rendered pages — deliberately not worked
  around), Rough Trade, HMV and Norman Records (403). Anything else that isn't Shopify would need its own reader like
  Juno's.

**Who you follow.** On first run the top **200** artists you listen to, own on vinyl, or have seen live (by score) are
followed from Music Companion — read-only, its database is never modified. Placeholder "artists" like *Various* /
*Various Artists* / *[unknown]* are never followed. Curate in **Settings → Music & news**: *pin* an artist to check them
first, *mute* to hide everything about them, add others by name, or *Resync* any time (a resync only adds and refreshes
scores — it never un-follows anyone or undoes a mute).

**Curating.** Every row can be **dismissed**, have its artist **muted**, or be **pinned** (buttons appear on hover in
that order; a pinned row keeps its pin lit and an accent bar so it's obvious). Dismissed items can be restored from the
expanded view's *Dismissed* filter. The first look at an artist or store is a silent baseline: only things that appear
*afterwards* are flagged **New**, and a refresh never undoes your pins or dismissals.

A fresh install works through all followed artists in the background (several minutes, politely paced). The Ticketmaster
key is write-only — the browser never sees it again.

## The Music page

Playlists Citadel designs from your own listening (Music Companion's database, read-only) and keeps on your Spotify
account, plus a player bar that steers Spotify wherever it's playing — the desktop app on this PC, a phone, a speaker.

- **Playlists** — each is a design: *ingredients* with a share each (your history, a time capsule, your records'
  tracklists, deep cuts, picked artists, a genre, a gig setlist, new releases, Last.fm similar artists, MusicBrainz band
  connections, a Last.fm tag) and *rules* (length, max per artist, familiar ↔ new, skip recent plays, no repeats, order).
  **Fixed** playlists change only when rebuilt; **rotating** ones are rebuilt on schedule into the *same* Spotify
  playlist (so its link and followers stay). A scheduled rebuild keeps the previous local and Spotify playlists
  if an ingredient or Spotify matching fails, or no playable tracks are found; the failure is shown on the playlist
  and retried after the usual back-off. An ingredient returning no songs successfully can still be filled from
  the others. Post-gig setlist updates use the same safeguard, keeping the prediction and its locks until a replacement
  is ready. Manual rebuilds can still show a partial draft with warnings for you to review before saving.
  Lock a track to keep it through rebuilds; remove one and that playlist
  leaves it out for good. Songs Spotify can't match are listed with a **Find…** to pick by hand (remembered).
- **Discover** — artists you've never (or barely) played, drawn from your top 100 by Last.fm similarity and MusicBrainz
  shared members, weighted by how much you play the artists they connect to and spread so no one vibe or artist fills
  the list, with your artists' solo careers and side projects left out (MusicBrainz memberships); "% match" is
  Last.fm's own similarity to the artist of yours they're closest to. **By vibe** groups five
  new artists under each style your top 100 spans (Reggae & Ska, Drum & Bass, Desert & Stoner Rock…), the ones you
  play least first. **Rediscover** lists artists you've drifted from, with a one-click mix. A result is kept until a
  newer one replaces it (a restart or an update never throws one away); the *Discover* job redoes stale ones (a week
  old, cut short, or from older code) at 03:00, one at a time, and **Refresh** queues one by hand at the same pace.
- **Gigs** — a predicted setlist for each booked gig (from the band's last ten shows on Setlist.fm), a **pre-gig
  playlist** that refreshes daily and switches to the real setlist after the show, playlists of shows you were at, and
  any setlist.fm link → playlist (what Setlist → Spotify did).
- **Releases / Vinyl** — Home's cards given the whole page, with **Listen** (plays the album, or before release day their
  biggest album). Pins, dismissals and purchases are shared with Home.

The player bar has three sizes (full, slim, hidden into a chip in the page header); on the fixed 1080p/1440p layout
it has its own row and never covers anything.

**Setting up** (Settings → **Music page**): your Spotify app's client ID/secret (developer.spotify.com — one app per
account, Premium needed), with `http://127.0.0.1:8080/music/spotify/callback` added to its Redirect URIs; then
**Connect**, finishing the login *in a browser on the Citadel PC* (Spotify only returns logins to 127.0.0.1). A login
lasts six months whatever happens, so Citadel warns two weeks ahead. Last.fm and Setlist.fm keys go in the same tab.

Spotify's 2026 rules for personal apps decided several things here: no recommendations / related artists / audio
features / artist top tracks (Last.fm and MusicBrainz stand in), search returns at most 10 results (one song at a time,
remembered), playlists are made with `POST /me/playlists` and filled via `/playlists/{id}/items`.

**Staying within Spotify's limits** (its [rate-limit guidance](https://developer.spotify.com/documentation/web-api/concepts/rate-limits)
and Developer Terms). Spotify counts each app's calls over a rolling 30 seconds and doesn't publish the limit; a
personal app's is low (a Discover rebuild at ~2 searches a second earned a 19-hour ban on 2026-10-07). So:
- **One pacer** (`app/music/services/spotify.py`): at most 15 calls in any 30 s, of which background work (Discover,
  playlist matching, rotations) gets 5, one every 6 s, and only while the page has room to spare.
- **Back-off**: a 429 (or 503) is never retried. Citadel waits as long as `Retry-After` says — or 1 min, doubling with
  each refusal in a row up to a day, whichever is longer — and sends *nothing* to Spotify until then, restarts
  included. The page says when it'll pick up again.
- **Lazy, not polled**: the player is asked just after the current track ends (or every 30 s, to notice a change
  made elsewhere), every 30 s when paused, every minute when nothing's playing, and never while the page is hidden;
  its clock ticks locally. Answers are shared between open pages for 4 s.
- **Albums, not top 10s**: Discover finds each artist's album (Last.fm's biggest, else Spotify's first) with one
  search, kept for a month, and Play/Queue reuse it without searching again. Spotify's batch endpoints (Get Several
  Albums/Tracks/Artists) and artists' top tracks were removed for personal apps in February 2026, so one search per
  artist is the floor. Queue is the costliest button (Spotify queues one track per call), so it only ever runs when pressed.
- **Least privilege**: only the scopes in use are requested (they apply from the next Connect).
- **Attribution**: Spotify's full logo (white on dark themes, black on light, ≥70 px) sits beside its content in the
  player bar and the Find… results, linking to the track on Spotify; its artwork is shown uncropped with 4 px corners.

**Adding a service**: one module in `app/music/services/` (a `Service` with its fields and a `test()`), usually plus an
ingredient in `app/music/ingredients.py` that uses it. Settings lists it by itself.

## Widgets

| Widget | Source | Notes |
|---|---|---|
| Now / bins | Salford City Council website | needs your property's UPRN; shows next recycling (blue/brown alternate) and next domestic |
| Diary | Google Calendar secret iCal address | read-only; Google can lag a few hours refreshing these feeds |
| System | host `/proc` + the local `disk-usage-server` | falls back to `statvfs('/')` |
| Projects | systemd `--user` transient units `hub-proj-<slug>` | survive dashboard restarts; logs via the journal |

A failed refresh keeps the last good data and shows a *stale* badge.

## Schedule, maintenance & logs

Settings → **Maintenance & logs** shows every job, when it last ran, what it did, when it runs next, and lets you change the
schedule or **Run now**.

**Sync schedule.** The data syncs are **daily, overnight, one after another**, so nobody's service is hammered:

| Job | Default | Notes |
|---|---|---|
| Bins | daily 03:00 | the council page changes about weekly |
| Releases | daily 04:00 | MusicBrainz, 1 request/s → ~5 min for 200 artists |
| Vinyl radar | daily 04:30 | five Shopify stores (a few requests each); Juno (~100 slow page requests, ~10 min) at most once a day |
| Gig radar | daily 05:00 | Ticketmaster, ~200 calls (limit 5,000/day) |
| Headlines | every 3 h | a handful of RSS requests |
| Diary | every 30 min | one small request to your private calendar address |
| Gigs, System | local | nothing outside this PC is contacted |

- **Restarts don't re-run anything.** What is due comes from the record of when each job last succeeded, not from when the
  service started. A job missed while the PC was off runs when it comes back (jobs overdue at start-up are staggered).
- **Each artist is looked up about once a day**, however often you press *Sync now* (which skips anyone checked in the last hour).
- **After a failure** a job retries after 5 min, then 15, 30, 60, and at most every 2 hours — it heals on its own without hammering.
- **You can't set the heavy jobs to poll every few minutes**: each has a floor (6 h for Releases / Vinyl / Gig radar).

**Housekeeping (nightly).** 02:30 **database backup** (a consistent, integrity-checked copy in `data/backups/`, the last 7 kept, owner-only
permissions since it holds the same secrets as the database — restore by stopping the service and copying one over `data/hub.sqlite`);
02:45 **tidy** (old log entries, stale items, expired enrolment links); 06:00 **health check** (database integrity, disk space, and
every integration: it warns when a job has failed 3 times running or hasn't succeeded for a schedule-and-a-half).

**Event log.** Everything noteworthy is written to the database and shown in Settings (filter by *Problems / Everything / Errors only*,
by source, or search): each sync's result, every failure (with its attempt number), a store or API that has *changed* (Juno's page
returning no records, a Shopify collection that empties, MusicBrainz or Ticketmaster answering in a different shape), and any
unexpected error with its traceback. Kept 30 days (max 5,000 entries). **Secrets are stripped before anything is stored or shown**
— API keys in URLs, private calendar addresses, bearer tokens, one-time enrolment links, and any value a widget declares secret.
A failing job also puts an alert in your dashboard's top strip (owner only) once it has failed twice in a row, and a red dot on the
Maintenance tab.

## Operating it

```bash
journalctl --user -u hub -f          # logs (calendar/other secret URLs are never logged)
systemctl --user restart hub         # restart (running projects keep running)
deploy/install.sh                    # update deps / units after pulling changes
deploy/uninstall.sh                  # remove service + `hub` (keeps data/ and .env)
```

## Remote access (Tailscale)

Nothing is exposed to the internet. Away from home, devices reach the LAN over Tailscale: the host is a **subnet
router** for `192.168.0.0/24`, so every LAN address (Citadel, the Music Database servers, Jellyfin on `.70`) works
unchanged — same URLs, same CSP, same enrolled-device cookies.

Host setup (once, needs sudo):

```bash
sudo pacman -S tailscale
echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-tailscale.conf && sudo sysctl --system
sudo systemctl enable --now tailscaled
sudo ufw allow in on tailscale0 comment 'Tailscale: my own devices'
sudo ufw route allow in on tailscale0 to 192.168.0.0/24 comment 'Tailscale subnet route to LAN'
sudo tailscale up --advertise-routes=192.168.0.0/24 --operator="$USER"
```

Then in the admin console (login.tailscale.com/admin/machines), on the host: **Edit route settings** → approve
`192.168.0.0/24`, and **Disable key expiry** (otherwise it drops off after ~180 days).

Clients: install Tailscale and sign in with the same account. Linux clients need `sudo tailscale up --accept-routes`;
Windows, macOS, iOS and Android accept the route automatically. A device still needs `hub enrol` to be an owner —
being on the tailnet only makes it a LAN visitor. If a remote network also uses `192.168.0.x`, LAN addresses clash;
use a phone hotspot instead.

## Moving to another PC

1. Copy the folder (including `data/hub.sqlite` and `.env`) — or run `git clone` and copy those two.
2. Set `HUB_PROJECTS_DIR` in `.env` if the projects live somewhere else, and check each project's paths in the app.
3. `deploy/install.sh`, then the two `sudo` lines above. Devices you enrolled keep working (the cookie key is
   in `data/secret.key`; copy it too, otherwise re-run `hub enrol`).
4. Redo the Tailscale host setup above on the new PC (and remove the old one in the admin console).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # includes real systemd start/stop tests
.venv/bin/python -m app             # run in the foreground on :8080
```

Design system: `web/css/tokens.css` (black / purple / silver). Layout is fixed to one screen at 2560×1440 (scaled
×1.2, capped beyond that) and flows below 1800px; every card expands to a fuller view.
