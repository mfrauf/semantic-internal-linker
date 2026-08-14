# Google Sheets/Drive REST notes (raw API, no gws CLI)

Used 2026-08-10 to create the dedicated v1 spreadsheet for the internal
linker. Auth: OAuth token `google_token.json` — scopes include
`drive` + `spreadsheets`, so Drive file ops work with the same token.

## Create a spreadsheet + tabs programmatically

1. Create: `POST https://sheets.googleapis.com/v4/spreadsheets` with body
   `{"properties": {"title": "..."}}`.
   **Response-shape gotcha**: sheet metadata is nested under `properties` —
   read `created["sheets"][0]["properties"]["sheetId"]`, NOT `["sheetId"]`.
   Reading the wrong level raises `KeyError` AFTER the file was already
   created, leaving an orphan spreadsheet in Drive.
2. Rename the default tab + add another tab:
   `POST .../spreadsheets/{id}:batchUpdate` with
   `{"requests": [
     {"updateSheetProperties": {"properties": {"sheetId": N, "title": "articles"}, "fields": "title"}},
     {"addSheet": {"properties": {"title": "candidates"}}}]}`
   (`N` = the default sheet's id from the create response, usually 0.)
3. Write header rows:
   `PUT .../spreadsheets/{id}/values/{tab}!A1?valueInputOption=RAW` with body
   `{"values": [["url", "title", ...]]}`.

## Finding an orphaned spreadsheet (Drive files.list)

`GET https://www.googleapis.com/drive/v3/files` — **GET only, and ALL query
params must go through `urllib.parse.urlencode`**:
- `q=mimeType='application/vnd.google-apps.spreadsheet' and trashed=false`
  (note the single quotes inside q values)
- `orderBy=createdTime desc` — an unencoded space here crashes with
  `http.client.InvalidURL`
- `fields=files(id,name,createdTime)`
- **NEVER `POST /files` to search** — that endpoint CREATES a file.
- Name-based `q=name='...'` can lag on brand-new files; filter by mimeType +
  createdTime desc instead.
- Recovery pattern: REUSE the orphan (finish the tab setup on it) rather than
  creating a duplicate; deletion needs Drive scope and is usually unnecessary.

## Verification

- Import the target script's module and call its own readers
  (`read_sheet(new_id)`, `ensure_tab(...)`) against the new ID — proves the
  exact code path that will run.
- `grep` the old ID across scripts, wrappers, and docs afterwards: argparse
  defaults, docstrings, and shell `SHEET=` vars are the usual hiding spots.
