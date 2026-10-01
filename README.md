# onCORE Parcel Check

Upload a property-list CSV and see which parcels are already in HubSpot. Rows are matched on APN (scoped by county), owner entity, or mailing address. For each match you see:

- Lead status, and whether the parcel is already engaged
- **Mail undeliverable**: YES if any matched parcel has Mail Undeliverable = Yes
- **Date last mailed**: the most recent Date Last Mailed across the matched parcels

The results CSV adds `match_found`, `matched_on`, `matched_lead_status`, `already_engaged`, `mail_undeliverable`, `date_last_mailed`, `matched_hs_name` and `matched_hs_id`.

The app uses only the Python standard library, so there is nothing to install.

## Deploy on Railway

1. New Project → Deploy from GitHub repo → pick this repo. Railway detects Python and starts the app from the `Procfile`.
2. Under Variables, add:
   - `HUBSPOT_TOKEN`: a HubSpot private app token with read access to companies (`crm.objects.companies.read`, `crm.schemas.companies.read`)
   - `APP_PASSWORD`: the shared team password for the link
   - `APP_SECRET` (optional): any random string. It signs the login cookie, and changing it logs everyone out.
3. Under Settings → Networking, click Generate Domain. That URL is the link for the team.

When the app starts, it pulls every parcel from HubSpot in the background and refreshes them every 60 minutes. Checks run against that in-memory copy.

### Settings

The defaults already match the onCORE portal:

| Setting | HubSpot property |
|---|---|
| APN | `apn_1` |
| Owner | `owner` |
| Mailing address | `mailing_address` |
| Lead status | `hs_lead_status` |
| County | `county_name` |
| Mail undeliverable | `mail_undeliverable` |
| Date last mailed | `date_last_mailed` |

The engaged statuses also come preset. Changes made on the Settings tab are saved to the server's disk, and Railway wipes that disk on every redeploy. To keep changes permanently, attach a Railway volume and set `CONFIG_DIR` to its mount path, or change `DEFAULT_CONFIG` in `app.py`.

## Run locally

```
python3 app.py
```

The app opens at `http://127.0.0.1:8765`. Set `HUBSPOT_TOKEN`, or paste a token on the Settings tab.

## Matching notes

- APNs are compared without punctuation or leading zeros. Values Excel wrote in scientific notation are expanded back to digits.
- Owner names are compared without legal and trust noise words (LLC, Trust, Revoc, Living, Ttees, etc.) and without regard to word order.
- Addresses are compared on the street line only, meaning everything before the first comma. A list's `5789 RYAN RD, DULUTH, MN 55804` matches HubSpot's `5789 Ryan Rd`. The trade-off is that two parcels with the same street address in different towns will also match, so `matched_on` tells you which key triggered each match.
