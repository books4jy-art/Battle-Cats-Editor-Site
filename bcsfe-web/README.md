# Battle Cats Save Editor (web)

A website for [BCSFE-Python](https://codeberg.org/fieryhenry/BCSFE-Python), the Battle Cats
save file editor by fieryhenry. Styled to match Sky Mesh Explorer.

- **Transfer code tab:** enter a transfer code, a confirmation code and a country, then pick the edits.
  The site downloads the save, edits it, uploads it, and shows **new codes**. It also offers the
  original save as a backup download.
- **Save file tab:** drop in a `SAVE_DATA` file and download the edited file. Nothing is uploaded to
  the game servers in this mode. Leave the edits empty to just see what's in the save.

Edits: Cat Food, XP, NP, Leadership, Cat Tickets, Rare / Platinum / Legend Tickets, Platinum Shards,
unlock all obtainable cats, true-form unlocked cats, and max battle items / Catseyes / Catamins /
treasure chests, clear main story chapters (Empire of Cats, Into the Future,
Cats of the Cosmos) and set their treasures (none / inferior / normal / superior).
The collapsible **Stages** section can also clear every map of Stories of Legend, Uncanny Legends,
Zero Legends, event stages and collab stages that exists in your game version, up to a chosen number of
crowns.
The collapsible **Characters** section adds or removes individual characters (search by ID, ID range or
name, using the names in your region's game data) and adds whole rarities (obtainable characters only).
Its **Level up** box sets levels like `50+10`, `30`, `+20` or `max` for all owned characters or for
characters picked with the **Lv** button, capped at each character's limit.
The collapsible **Talent orbs** section sets how many of each orb type you have, for all orb types or
by grade / trait / effect (only orb types that exist in your game version).
Separate collapsible sections set the amount of each **Catfruit & seed**, **Behemoth stone & gem** (the
"crystals") and **Catseye** type: one amount for all types or for the types you pick, like the talent orbs. There's also an option to upload to a new account ID (BCSFE's strict ban prevention).

## Run it on your computer

You need Python 3.9 or newer (<https://www.python.org/downloads/>). On Windows, tick
**"Add Python to PATH"** during install.

- **Windows:** double-click `start.bat`
- **Mac / Linux:** run `./start.sh`

Your browser opens at <http://localhost:8000>. Keep the black window open while you use the site.
Only you can reach it at this address. To let other people use it, host it (below).

## Put it online (works on any device)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/books4jy-art/Battle-Cats-Editor-Site)

1. On GitHub, create a **public** repository named `Battle-Cats-Editor-Site`. Click
   **uploading an existing file**, drag in everything from this folder (including the `static`
   folder), and click **Commit changes**.
2. Click the **Deploy to Render** button above (or open
   <https://render.com/deploy?repo=https://github.com/books4jy-art/Battle-Cats-Editor-Site>).
   Sign in to Render with GitHub, then click **Deploy Blueprint**. `render.yaml` fills in all the settings.
3. After a few minutes Render shows a link like `https://battle-cats-editor-site.onrender.com`.
   Open it on any phone, tablet or computer.

On the free plan, the site sleeps after 15 minutes without visitors. The first visit after that
takes about a minute to load.

If you named the repository something else, change `Battle-Cats-Editor-Site` in the link to match.

## How it works

- `static/index.html`: the whole page (HTML, CSS and JS in one file, no build step).
- `app.py`: a small Flask server. `POST /api/edit` checks the input, rate-limits per visitor
  (one edit at a time plus a cooldown), and limits how many edits run at once.
- `worker.py`: runs each edit in its **own process**, calling BCSFE directly
  (`ServerHandler.from_codes` → edit → `ServerHandler.get_codes`). Isolating each edit keeps
  visitors' saves from mixing and stops one crashed edit from taking the site down.
  Transfer codes and saves are never written to logs, and each edit's temporary files are deleted.

Settings are optional. See `.env.example`.

## Notes

- **Ban risk:** editing Cat Food and premium tickets can get accounts banned. The site reports those
  changes to the server the same way the BCSFE CLI does, but that is not a guarantee. Editing saves
  is against the game's terms of service, and users do it at their own risk.
- Cat unlocking and true forms need BCSFE's game data, which is downloaded on first use and cached.
- BCSFE is GPL-3.0, so if you publish this site's code, it should be GPL-3.0 too.
