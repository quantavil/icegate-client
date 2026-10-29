# icegate-client

Licensed under GPL-3.0-or-later; see [LICENSE](LICENSE).

Browser-free client for ICEGATE, eSANCHIT and electronic filing. It logs in with the same
endpoints the portal's own JavaScript uses, refreshes the dashboard token,
performs the dashboard to eSANCHIT handoff, uploads already-signed PDFs, and
can submit one staged batch to obtain the DRN and IRN. Electronic filing
uploads already-signed SB/BE files and reports the portal's initial response.

See the [file-upload research](docs/electronic-filing.md) for observed portal
behavior and the distinction between upload acceptance and Customs processing.

**Staging uploads files; submission creates portal records.** `icegate submit`
requires a challenge string derived from the file hashes before it sends
anything. Staging is scoped to the current eSANCHIT session: the queue is empty
again on the next handoff, so upload and submit must happen in one process.

> A two-PDF live submission succeeded on 2026-10-29 and its DRN and IRNs
> matched the confirmation email. Other response shapes may still report
> `unknown` even when accepted. The client never retries a submit.

## Why this exists

The portal keeps its authentication state in `sessionStorage`, so a browser
restart means a fresh login, and the eSANCHIT session expires after roughly ten
minutes of inactivity. This client removes the browser from the loop:

- no browser process, profile or cookie store;
- credentials read from a local mode-0600 file;
- direct login with the configured ICEGATE credentials;
- a journal designed to redact secrets.

## Protections

| Rule | How it is enforced |
|---|---|
| Stage never submits | `FORM_ACTION_ALLOWLIST` holds only `uploadDocs`; every POST passes an explicit allowlist |
| Submit needs a typed challenge | `SUBMIT <n> <plan_id>` covers the file hashes, sizes and codes; a changed file changes the challenge |
| Avoid repeat submit | one POST per attempt, a state-directory lock and a receipt per plan; these cannot detect submissions made elsewhere |
| Reduce secret exposure | journal fields and normal console output use the redactor; handoff URL query strings are removed |
| Never disable TLS verification | `verify` is always a CA bundle path, never `False` |
| Never retry a POST | only `GET`/`HEAD` are retried, once |
| Never store tokens | token and `JSESSIONID` live in memory for the life of the command |

## Install

```bash
cd ~/Documents/icegate-client
uv sync
```

## Configure

```bash
uv run icegate init           # writes .env in the project root (mode 0600)
$EDITOR .env
uv run icegate doctor
```

The client uses the ignored `icegate-client/.env` by default when run from the
source checkout. It must not be group- or world-readable. An installed wheel
uses `.env` in the current directory by default.

```
ICEGATE_USERNAME=your-id
ICEGATE_PASSWORD=your-password
```

Environment variables with the same names override the file, which is handy for
one-off runs. `ICEGATE_STATE_DIR` and `ICEGATE_TIMEOUT` are optional.
To use another file instead of the default, pass `--env-file PATH` to each
command.

## Usage

```bash
uv run icegate login                  # authenticate and probe the dashboard
uv run icegate status                 # authenticate, rotate the token, probe again
uv run icegate esanchit handoff       # open eSANCHIT and list staged rows
uv run icegate esanchit doc-types "invoice" # search the bundled document codes
uv run icegate esanchit stage a.pdf b.pdf --doc-type 331000
uv run icegate esanchit stage a.pdf --dry-run  # local validation only
uv run icegate esanchit submit a.pdf --doc-type 331000 --dry-run
uv run icegate esanchit submit a.pdf --doc-type 331000 --confirm 'SUBMIT 1 <plan_id>'
uv run icegate filing upload signed.sb --dry-run
uv run icegate filing upload signed.sb --confirm 'FILE 1 <plan_id>'
uv run icegate ecopy search SB --from 2026-10-01 --to 2026-10-28
uv run icegate ecopy search SB --yesterday
uv run icegate ecopy search BE --today
uv run icegate ecopy search BE --location INMBD6 --job-number 123 --job-date 2026-10-28 --json
```

`ecopy search` retrieves Shipping Bill (`SB`) or Bill of Entry (`BE`) records
by date range, or by customs location, job number and job date. Dates use
`YYYY-MM-DD`. Add `--json` for the complete portal records, including fields
for available copies; the table shows job, document and location details.
`--today` and `--yesterday` use the calendar date in India. `--yestarday` is
also accepted as an alias for `--yesterday`.
Use either a relative-date flag, a `--from`/`--to` pair, or all three job
fields; they cannot be combined. The JSON `downloadUrl` field is a portal
filename, not necessarily a complete URL.
The portal may restrict the permitted date range. Search reads records and does
not download the document copies. On 2026-10-29, live searches by both date and
job returned records for both SB and BE through this CLI.

`filing upload` accepts `.sb` and `.be` files containing signature and
certificate sections. It checks the section structure only: it does not
cryptographically verify the DSC or sign the file. `accepted_for_processing`
is the initial portal response, not a Customs filing result. Check the separate
ICEGATE status email. Each file is sent once, and an attempt receipt prevents
automatic resubmission; an uncertain response must be reconciled manually.
For a batch, processing stops at the first failed or unknown response and
remaining files are not attempted. The challenge printed by the dry run must
be copied exactly into the upload command. A local receipt prevents repeat
uploads only within the same state directory.

`submit` is two runs by design. The first prints the plan and a challenge; the
second, with `--confirm`, authenticates, hands off, uploads, re-checks that the
portal shows exactly the planned rows, and posts one `submitDocs`. Exit codes:
`0` verified response, `3` outcome unknown, `4` confirmation missing. Local
validation failures stop before submission.

`stage` performs the whole flow in one process: login, handoff, upload, then a
report. It prints the staged rows (file, start time, signature verdict, the
portal's own type field) and exits. `--doc-type` and `--doc-type-map` are
*plans*: the client checks each code against the row's real option list and
prints it, but the portal only persists a type when it submits.

Useful `stage` flags:

| Flag | Meaning |
|---|---|
| `--doc-type CODE` | plan one document code for every file |
| `--doc-type-map FILE=CODE` | per-file plan, repeatable |
| `--dry-run` | validate files and stop before any network call |
| `--append` | allow uploading when rows are already staged |
| `--allow-unsigned` | do not fail on missing signature markers |
| `--strict-limits` | treat portal size and count limits as errors |
| `--dump-html PATH` | save the post-upload page (mode 0600) for debugging |

Workflow commands accept `--json`, `--env-file`, `--state-dir`,
`--no-journal` and `-v`.

The previous top-level `handoff`, `stage`, `submit`, and `doc-types` commands
remain available for compatibility.

## What the client checks before uploading

- `%PDF-` header and at least one page;
- not encrypted;
- a signature field, validated with **pyHanko**: all signature digests and CMS
  values must verify and the last signature must cover the entire file;
- file count, per-file size and duplicate names.

Trust is separate from integrity. The PDF validator currently has no trust
roots configured, so it cannot establish signer trust.

`stage` warns if a signature cannot be parsed; `submit` refuses by default.
A signature that parses but fails validation is rejected by both commands.
Both accept `--allow-invalid-signature` as an explicit override. A rejected
submission may still leave a record.

## Limits: what each source says

The published numbers disagree, so they are taken from the live page rather than
from documentation. Everything below was read on 2026-10-28.

| Source | Per file | Per batch | Notes |
|---|---|---|---|
| ICEGATE FAQ (`/help/faq`) | "lesser than 1 MB" | "max 5 documents" | stale; the FAQ also still says the 1 MB figure |
| eSANCHIT step-by-step PDF (2022-04) | 1 MB | "maximum 5 documents" | stale |
| Upload dialog, live page text | "Maximum size per file is 2 MB." | "The total number of maximum files are 5 per DRN." | current |
| Upload dialog, live JS | `maxSize = parseInt('2097152')` = 2 MiB | `upoadSize > 10485760` for a broker account | per-request cap, not per batch |

Three details that the visible text does not tell you:

- **The client-side per-request cap is 10 MiB, despite the 50 MB message.** The
  JS has one branch per user class: 52 428 800 bytes for agency users and
  10 485 760 for broker accounts, but both branches print "Maximum of 50 MB is
  allowed per request." Five 2 MiB files already reach the 10 MiB ceiling, so
  this is a client-side check; server enforcement has not been established.
- **The five-file check does not actually run.** `numberOfFileCheck()` reads
  `documentBean.maxFile`, which the server never substitutes, so the comparison
  is a number against the jQuery function and is always false. The rule is a
  sentence in the help text, not an enforced client limit. Whether the server
  enforces it is unknown.
- **The dialog accepts PDF/Doc/Docx/jpg/jpeg; this client still only sends
  signed PDFs.** Filenames must be unique, which preflight enforces.

Practical numbers used by the client: 2 MiB per file, 5 files per DRN and the
live page's per-request cap. The page is re-read after the handoff, so a change
there is honoured without a release. `--strict-limits` turns the size and count
warnings into errors.

## Protocol notes

Everything below was observed against the live portal on 2026-10-28.

| Step | Request |
|---|---|
| Login | `POST https://foservices.icegate.gov.in/identity/ext-login` |
| Refresh | `POST /identity/refresh-token` (only before expiry) |
| Dashboard | `POST /dashboard/services/get` |
| Handoff | `POST /esanchit/esanchitDynamicUrl` |
| Upload | `POST https://www.icegate.gov.in/esanchit/uploadDocs` |
| Submit | `POST https://www.icegate.gov.in/esanchit/submitDocs` |

- The password field is base64 of AES-128-ECB/PKCS7 using the key published in
  the public bundle. It hides the password from casual inspection in the
  request body; it is not a secret.
- The dashboard token lasts exactly ten minutes. Activity does not extend it.
  Refresh issues a new ten-minute token, but only while the old one is valid;
  after expiry the API returns HTTP 409 `auth error..!!!`.
- The handoff ticket is a bearer value in the query string. It must be used
  immediately and never logged.
- `www.icegate.gov.in` sends only its leaf certificate and omits the issuing
  intermediate. The client appends `Entrust DV TLS Issuing RSA CA 2` to the
  `certifi` bundle at runtime. Verification stays on.
- Redirect chains answer with `http://` URLs. Each hop to an `icegate.gov.in`
  host is upgraded to HTTPS before the request is made.
- `uploadDocs` returns the staging page directly. Its rows carry read-only
  `DRN`/`IRN`/`digiSignValidated` inputs that stay empty until `submitDocs`,
  which is also the only action that reads `detailBean[N].docTyp`.

## Staging is ephemeral

A live upload on 2026-10-28 confirmed the shape of the queue:

- `uploadDocs` answers with one row per file, but `DRN`, `IRN`,
  `digiSignValidated` and the document type are read-only inputs that are still
  empty or unset after the upload.
- The row's type widget is a plain `<select
  name="detailBean[N].docTyp">` with no `onchange` handler and no save
  endpoint. Its `NONE` option means "not chosen".
- The queue is bound to the eSANCHIT `JSESSIONID` and `tabId`. A fresh handoff
  opens an empty page, so rows staged by a previous command are gone.

In short: staging validates the file and parks it in the current session. DRN,
IRN, the signature verdict and the chosen document type are created only by
`submitDocs`, which `icegate submit` posts in the same process.

## Document type planning

The portal's document-type options are full strings such as
`101000-Registration Document`. `icegate doc-types` searches the same catalogue
locally. `--doc-type` / `--doc-type-map` cross-check the requested codes
against the options the row itself offers, then print them in a
`planned type (not saved)` column.

Nothing is POSTed for a type. A requested code that the row does not offer is
reported as a warning, and the upload is kept.

## Submit: one attempt per plan

The portal has no undo, so submission is a state machine rather than a request:

| Step | What happens |
|---|---|
| plan | files are hashed, codes resolved, `plan_id = sha256(name,size,sha256,code)[:12]` |
| challenge | the user must pass `--confirm 'SUBMIT <n> <plan_id>'` |
| lock | `filelock` on `<state-dir>/submit.lock`; a second process is refused |
| receipt check | `<state-dir>/receipts/<plan_id>.json` exists means this exact plan already ran |
| upload | the validated file snapshot is checked against the plan, uploaded, and the resulting rows compared to the plan |
| submit | one `POST submitDocs` carrying `tabId` and `detailBean[N].docTyp` |
| dispatch guard | write an `unknown` receipt before the submit request, so an interrupted process blocks a repeat |
| result | `submitted` (matching rows, 16-digit DRN and IRNs, and verified signatures) or `unknown` |

`unknown` means the request may or may not have been accepted: a timeout, a
5xx, or a 200 without a DRN. The client writes the receipt, prints where it is
and exits 3. It never retries, because a retry can create a second DRN.

Receipts are JSON, mode 0600, one per plan. `--allow-duplicate` is the only way
to submit the same plan again, and it has to be typed on purpose.
This guard only covers the same plan in the same state directory; it cannot
detect submissions made elsewhere or plans with different names or codes.
Check confirmation mail for existing uploads before submitting.

These local protections reduce duplicate risk; they cannot guarantee exactly
once processing by the portal, or detect a previous upload under a different
plan or state directory.

## Known limits

- Browser-free login worked without an OTP challenge in live tests. Per the
  user's request, the client has no OTP fallback; rejected logins stop.
- The absolute server-side session cap beyond 35 minutes of refresh has not
  been measured.
- Staged rows do not survive a new eSANCHIT session, so upload and submit must
  run in one process; `icegate submit` does that by design.
- The reusable client's two-file submission succeeded on 2026-10-29: both
  response rows reported successful signature verification and their DRN and
  IRNs matched the ICEGATE confirmation email, including both document types.
  Other response shapes and automated reconciliation remain unverified.
- No delete, no beneficiary editing, no CSV tracking.

## Journal

Each run writes `~/.local/state/icegate-client/runs/<timestamp>-<pid>.jsonl`,
mode 0600. It records intent (file names, sizes, SHA-256, document codes),
protocol outcomes (status codes, hop paths, query parameter *names*), and
staged row identifiers. It never records tokens, session ids, OTPs, passwords,
cookies or handoff URLs with their query strings.
