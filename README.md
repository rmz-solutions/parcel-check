# Parcel Check

Upload a new property list (CSV) and instantly see which parcels are **already
on file in HubSpot** — matched by APN, owner entity, or mailing address — and
what their current lead status is.

This version ships as a **double-click desktop app**. The everyday user installs
nothing: they just open the app, drop in a CSV, and download results. Python is
only needed *once*, by whoever builds the app.

---

## Part 1 — Build the app (one time, per operating system)

> **Who does this:** ONE technical person, once. Everyone else just receives the
> finished app and never touches anything in this section. If a teammate hit an
> error like "No developer tools were found" or "Could not create build
> environment," they were running this builder by mistake — they only need the
> finished `Parcel Check.app` / `Parcel Check.exe`, not these files.

Done once by someone comfortable opening a terminal. Needs Python 3
(<https://www.python.org/downloads/>). Keep all the files in this folder together.

### On a Mac → makes `Parcel Check.app`
1. Open **Terminal** (⌘+Space, type "Terminal").
2. Type `bash ` (with a trailing space) — don't press Enter yet.
3. Drag **`build_mac.command`** into the Terminal window, then press Enter.
4. Wait ~1 minute. When it finishes, the **`dist`** folder opens — your app is
   `dist/Parcel Check.app`.

> First-time Macs need Apple's **Command Line Tools** to build (a one-time ~5 min
> install). If a dialog asks to install them, click **Install**, wait, then run
> the build again. (Or run `xcode-select --install` in Terminal first.)

### On Windows → makes `Parcel Check.exe`
1. Double-click **`build_windows.bat`**. (If Windows blocks it: **More info →
   Run anyway**.)
2. Wait ~1 minute. The **`dist`** folder opens — your app is
   `dist\Parcel Check.exe`.

### Hand it out
The build creates **`dist/Parcel Check.zip`** — a proper macOS archive. Send that
**zip** (AirDrop/Drive/Slack); teammates double-click it to unpack
`Parcel Check.app`, move it to Applications, and double-click. **They install
nothing.** Send the zip, *not* a loose `.app` — a bare `.app` often arrives broken
("can't be opened"). (Windows: send the single `Parcel Check.exe`.)

> **If a teammate sees a window full of gibberish text** (`__PAGEZERO`, `__TEXT`,
> random symbols), they opened the program’s internal file instead of running the
> app. Just double-click **`Parcel Check.app`** (the item with the rounded app
> icon) — never a plain file named `Parcel Check` with no `.app`.

> **If a Mac says the app simply "can't be opened"** (plain Finder dialog, just an
> OK button — not the malware/verify one), the app bundle arrived **broken in
> transfer** (lost permissions, or sent as a loose `.app`). Fix: rebuild, then
> send the **`Parcel Check.zip`** the build now creates (it's a proper macOS
> archive that transfers intact). The teammate double-clicks the zip to unpack the
> app, then opens it. If macOS still blocks it on first launch, in Terminal run
> `xattr -cr ` (trailing space), drag the app in, press Enter, then double-click.

> **Unsigned-app warnings (expected, not malware):** the app isn’t code-signed,
> so the first launch on each machine is blocked by the OS. One-time, per machine.
> • **Mac (the “Apple could not verify… / Not Opened” dialog):** click **Done**
>   (never *Move to Trash*). Open **System Settings → Privacy & Security**, scroll
>   to **Security**, and click **Open Anyway** next to “Parcel Check was blocked,”
>   then confirm with your password/Touch ID. (Older macOS: right-click the app →
>   **Open** → **Open**.) If no *Open Anyway* appears, open **Terminal**, type
>   `xattr -dr com.apple.quarantine ` (trailing space), drag the app in, press
>   Enter, then double-click it.
> • **Windows:** **More info** → **Run anyway**.

---

## Part 2 — Connect it to HubSpot (one time, in the app)

### Make a read-only HubSpot key
HubSpot now recommends **Service Keys** for data-only integrations like this one
(they replace legacy private apps for this use case, and add key rotation, usage
logs, and account-level ownership). You’ll need **Super Admin** or **Developer
tools** access to create one.

In HubSpot: **Settings ⚙ → Integrations → Service Keys → Create a service key**
(in some accounts it’s under **Development → Keys → Service Keys**). Name it
`Parcel Check`, then grant **read** access to **Companies** and **company
properties / schemas** (the app only ever reads). Click create and **copy the
key** — it’s used as a Bearer token, exactly what this app expects.

> **Fallback:** Service Keys are in public beta. If you don’t see them in your
> account yet, a legacy **Private App** works identically — Settings →
> Integrations → Private Apps → Create a private app, with scopes
> `crm.objects.companies.read` and `crm.schemas.companies.read`. Either key
> pastes into the same field in the app.

Whichever you create is read-only — it can’t change or delete anything in HubSpot.

### Enter it in the app (Settings tab)
Open the app, click **Settings**, then:
1. Paste the token into **Access token**.
2. Click **Load properties from HubSpot**.
3. Choose the company properties holding your **APN, owner entity, mailing
   address, and lead status** (dropdowns show each field’s label + internal name).
4. *(Optional)* If you work across multiple counties, also pick your **County**
   property — APNs can repeat between counties, so this scopes APN matches to the
   same county. Leave it as “none” if you don’t need it.
5. Tick the lead-status values that mean **“already engaged”** (e.g. Interested,
   Signed, In contact).
6. Click **Save settings**.

You never type column names anywhere. When you upload a list, the app reads that
file’s headers and **locates the right columns itself**; you only touch it to fix
a wrong guess, and even then you pick from a dropdown of the file’s real headers.

Settings are saved on that computer (in a `.parcelcheck` folder in the user’s
home directory) and persist between runs. Change them anytime your HubSpot
fields change — no rebuilding needed.

---

## Part 3 — Everyday use (no install)

1. Open the app (double-click `Parcel Check`). It opens in your browser.
2. On **Check a list**, drop in your CSV. The app shows the **columns it located**
   in your file — glance at it, adjust only if a guess is off (via dropdown).
3. Click **Run check** — it syncs parcels from HubSpot, then matches your list.
4. Read the summary (e.g. *“142 / 500 already on file”*) and the table.
5. Click **Download results CSV**.
6. When finished, click **Quit Parcel Check** at the bottom (or close the app
   window).

The results CSV is your original file plus:

| Column                | Meaning                                                |
|-----------------------|--------------------------------------------------------|
| `match_found`         | YES / no                                               |
| `matched_on`          | which key matched: `APN`, `OWNER`, and/or `ADDRESS`    |
| `matched_lead_status` | the existing parcel’s lead status                      |
| `already_engaged`     | YES / no (based on the statuses you ticked)            |
| `matched_hs_name`     | the matched HubSpot company name                       |
| `matched_hs_id`       | HubSpot record id                                      |

**Reading a match:** an `APN` hit is essentially the same parcel (high
confidence). An `OWNER`- or `ADDRESS`-only hit means a *related* parcel — same
owner, maybe under a different entity — worth a quick human look.

---

## Notes

- **Where data goes:** nothing leaves the computer except read-only calls to
  HubSpot. The token is stored locally in the user’s `.parcelcheck` folder.
  Prefer not to keep it in a file? Set a `HUBSPOT_TOKEN` environment variable and
  the app uses that instead.
- **Matching is forgiving on purpose** — it ignores punctuation, word order,
  legal suffixes (LLC, Trust…), street-suffix abbreviations, units, and leading
  zeros in APNs.
- **Freshness & speed:** the first check pulls every parcel from HubSpot (tens of
  thousands = a few hundred API calls) and caches it on this computer, so checks
  are instant afterward. The cache **auto-refreshes hourly** — the first check
  after the hour is up re-pulls live data automatically, so you never act on data
  more than ~an hour old. You can also click **Refresh from HubSpot** anytime for
  up-to-the-minute data (e.g. right after a teammate adds parcels). Changing a
  setting that needs new data (like adding County) also triggers a refresh.
  *(Adjust the interval under **Settings → Auto-refresh interval** — default 60
  minutes; set it to 0 to cache until you manually refresh.)*
- **APNs and Excel:** if an APN column shows as scientific notation (e.g.
  `1.23E+11`), format that column as **Text** before saving the CSV (or import
  with the column type set to Text). Excel stores long numbers imprecisely and
  can permanently round digits. The tool defensively un-mangles scientific
  notation when the saved value still has full precision, but text is safest.
- **Multiple counties?** APNs can repeat across counties. Pick a **County**
  property in Settings and APN matches are automatically scoped to the same
  county — no other change needed.
- **No-build option:** if a builder already has Python and just wants to run it
  without packaging, `run_mac.command` / `run_windows.bat` (or
  `python3 app.py`) launch the same app.

### Files here
| File | What it is |
|------|------------|
| `build_mac.command` / `build_windows.bat` | build the desktop app (one time) |
| `app.py`, `ui.html`, `parcel_check.py` | the app’s source (bundled into the build) |
| `run_mac.command` / `run_windows.bat` | run without building (needs Python) |
