# AGENTS.md

Guidance for agents working in `icegate-client`. Read `README.md` first.

## Purpose

A browser-free ICEGATE client. It authenticates against
`foservices.icegate.gov.in`, keeps the dashboard token alive, performs the
dashboard to eSANCHIT handoff, uploads already-signed PDFs into the staging
queue, and can submit that queue in the same process to obtain DRN and IRN.
The separate electronic filing module uploads already-signed SB/BE files; it
does not sign or verify their DSC and does not claim final Customs acceptance.

**Staging is reversible; submission is not.** `icegate esanchit stage` can only reach
`uploadDocs`. `icegate esanchit submit` lives in `submission.py`, carries its own
allowlist, and refuses to run without a challenge derived from the file
hashes. Staging is ephemeral: the row lives in the current eSANCHIT
`JSESSIONID`/`tabId` and is gone on the next handoff.

## Non-negotiable invariants

1. **Submission is behind a gate.** `FORM_ACTION_ALLOWLIST` (staging) holds
   only `uploadDocs`; `SUBMIT_ALLOWLIST` lives in `submission.py` and holds
   only `submitDocs`. Never widen the stage allowlist. `submit` must keep the
   plan id (file hashes, sizes, codes), the typed challenge, the lock file, the
   duplicate-receipt check and the no-retry rule. A reconciliation step must
   never auto-resubmit.
   A durable `unknown` receipt is written immediately before the submit POST
   and replaced atomically after a result; duplicate receipts are checked again
   under the lock. The guard is local to a plan and state directory, so check
   existing confirmation mail as well. A complete, matching set of rows with
   one 16-digit DRN, distinct 16-digit IRNs and verified signature statuses
   is required to report `submitted`. A 4xx after dispatch remains unknown;
   it may be a failed follow-up after an accepted POST.
2. **No secret output.** Never print, log, journal or commit credentials, OTPs,
   dashboard tokens, session ids, cookies, `tabId`, or handoff URLs with their
   query string. Use `redact.scrub` / `redact.safe_url`. `Session.login()`
   registers the token and session id with the process redactor; do not bypass
   it with raw `print`.
3. **TLS verification stays on.** `verify` is always a CA bundle path from
   `tls.build_ca_bundle`, never `False` and never `verify=False` "just for a
   test".
4. **No automatic POST retries.** `IcegateSession.send` retries only `GET` and
   `HEAD`, once. A retried login can create extra sessions; a retried upload can
   duplicate a document.
5. **No token persistence.** Tokens and `JSESSIONID` live in memory for one
   command. Never write them to disk.
6. **Document types are never "saved" during staging.** The select is
   `<select name="detailBean[N].docTyp">` with no handler, and only
   `submitDocs` reads it. `--doc-type` produces a validated plan during
   staging; `submit` sends it as part of its single request. Do not invent a
   "save type" endpoint.

## Layout

| Path | Role |
|---|---|
| `src/icegate_client/shared/session.py` | login, token expiry, refresh, handoff ticket |
| `src/icegate_client/esanchit/client.py` | redirect chain, upload, per-call POST allowlist |
| `src/icegate_client/esanchit/page.py` | lxml page and form parsing |
| `src/icegate_client/esanchit/submission.py` | plan, challenge, lock, submit state machine, receipts |
| `src/icegate_client/esanchit/pdf.py` | local preflight and pyHanko signature validation |
| `src/icegate_client/shared/redact.py` | pattern and exact-value redaction |
| `src/icegate_client/shared/files.py` | private diagnostic files and atomic receipt writes |
| `src/icegate_client/shared/config.py` | env file and settings |
| `src/icegate_client/shared/tls.py` | certifi plus the pinned eSANCHIT intermediate |
| `src/icegate_client/cli.py` | commands and output |
| `src/icegate_client/esanchit/data/document_codes.txt` | bundled eSANCHIT codes |
| `src/icegate_client/filing/client.py` | signed SB/BE filing upload and per-file receipts |
| `src/icegate_client/ecopy.py` | read-only Shipping Bill, Bill of Entry and gatepass searches and downloads |
| `src/icegate_client/jobstatus.py` | read-only Shipping Bill Job Status detailed report |
| `src/icegate_client/myiec.py` | read-only My IEC client list |
| `src/icegate_client/enquiry.py` | public Shipping Bill enquiry, no login |
| `scripts/sign-and-plan.sh` | wrapper: copies named files to a temp dir, runs the private signer, writes `_Signed` files, then runs a `--dry-run` plan; never uploads |
| `tests/` | mocked tests, no network |

## Protocol facts (observed 2026-10-28)

For separate SB/BE electronic-file upload research and the 2026-10-29
authorized test, see [`docs/electronic-filing.md`](docs/electronic-filing.md).
One direct SB upload was accepted for processing; the later filing-status
email reported DSC validation failure. A deliberate repeat through the reusable
filing module also returned HTTP 202 accepted for processing. An email arriving
at the same time reported header validation failure for the same control
number; it lacks an upload identifier, so attribution remains uncertain.
The e-copy date and job searches were live-tested for both SB and BE using the
dashboard search endpoints. Signed PDFs for one SB and one BE were downloaded
through the CLI using `/esanchit/getDocument` with the record’s `irn` and
`userId`. The record's `userId` differs from the configured ICEGATE login;
searches cache the record's ID locally by account and IRN so downloads can
reuse it. Never replace it with the login ID. The separate `/dashboard-utility/download-utility/downloadAckFile`
endpoint returned HTTP 409 for six acknowledgement files; do not claim it
works until a successful response is observed. Date flags `--today` and `--yesterday` (also `--yestarday`)
use Asia/Kolkata calendar dates. A 27 October search returned four BE and 15
SB records; the portal's BE page showed those four alongside the 28 October
record when searched for 27–28 October. `downloadUrl` is a portal filename,
not necessarily a complete URL.

Read-only reports (observed 2026-10-30, live-tested):
- Gatepass (SB35) is an e-copy kind: `documentType: "LEOGate"` on the same
  `searchByDate`/`searchByJobNumber` calls; CLI kind `GATEPASS`. The list holds the
  same bills as SB; records without `irn`/`userId` have no gatepass yet. Download is
  the same `/esanchit/getDocument` call. Old bills answer HTTP 409 "available in next
  60 min" (queued retrieval); retry later. The PDF carries IEC, exporter, containers.
- `POST /dashboard/jobStatus/sbExcel` with `{icegateId, roleId,
  userType}` (lowercase user type from login). No date parameters: always the latest
  ~200 bills. The xlsx columns are Location Code, IEC, SB No, SB date, Status, Job No,
  Job Date, Error, Control Number; all are kept, empty ones as `""`.
- `POST /dashboard/myIec/getMyIecDetails/` with `{icegateId, roleId, iec, locationCode}`
  returns every client in one response (`locationCode`, `iecName`, `iecCode`); an IEC
  can repeat per location and can carry two names.
- `POST /enquiry/publicEnquiries/SBTrack_Ices_action_Public` with `{location, sbNo,
  sbDate: "YYYYMMDD"}`, no auth headers. One response holds every section, including
  empty ones. It also works for `INSTT6`, which the public form's list omits. IEC and
  GSTIN are masked by the portal.

- `POST /identity/ext-login` with `{icegateId, password, usertype:"external"}`.
  The password is base64 AES-128-ECB/PKCS7 with a key published in the SPA
  bundle. It is obfuscation, not a secret.
- Authenticated calls: `Authorization: <token>`, `session_id: <id>`,
  `channel: browser`.
- Timezone-less expiry strings are interpreted as India time; the portal does not explicitly specify a timezone.
- Token TTL is exactly 10 minutes. Refresh only works before expiry; after
  expiry the API returns HTTP 409 `auth error..!!!`.
- Handoff: `POST /esanchit/esanchitDynamicUrl` returns a URL with a bearer
  `DM_TICKET` query. Follow it with `allow_redirects=False`, upgrade every
  `http://` hop to HTTPS, and capture `JSESSIONID` on `www.icegate.gov.in`.
- Upload: `POST /esanchit/uploadDocs`, multipart, field `tabId` plus repeated
  `docs` files. The staging allowlist is exactly `{"uploadDocs"}`. One request
  carries the whole batch: the portal's own input is `<input multiple
  id="fileIn">`, so there is no per-file request and no resume. A rejection
  therefore applies to the batch, which is why the stage response is re-parsed
  for errors and every row is checked before submit.
- The upload page is also the authority on limits (`Maximum size per file is
  N MB`, `Maximum files are M`, and the `upoadSize >` comparisons in its
  JavaScript for the per-request cap). Preflight runs before the login and uses
  the documented defaults; both commands re-apply the live page's numbers after
  the handoff. Never hard-code a tighter rule.
- The published limits are stale: the FAQ and the 2022 step-by-step PDF say
  1 MB per file, the live dialog says 2 MB, and the live JS enforces 2 MiB per
  file plus 10 MiB per request for a broker account while printing "Maximum of
  50 MB". Do not "fix" 2 MiB down to 1 MB from documentation alone.
- The five-file rule is text-only: `numberOfFileCheck()` compares against
  `documentBean.maxFile`, which the server never substitutes, so the check is
  always false in the observed page. Treat 5 as advisory; the 10 MiB
  per-request check is client-side and server enforcement is unverified.
- The upload response already shows one row per file, but `DRN`, `IRN`,
  `digiSignValidated` and `detailBean[N].docTyp` are read-only/empty. They are
  populated only by `submitDocs`, which is also the only consumer of the
  document type.
- Staged rows are session-scoped. A new handoff (new `JSESSIONID`/`tabId`)
  renders an empty queue, so stage and submit must share one process.
- Submit: `POST /esanchit/submitDocs` must be the browser's whole form:
  multipart with an empty `docs` part, `tabId`, `finalSubmit` (empty), and per
  row `fileName`, `drn`, `irn` (empty) and `docTyp` set to the full option
  string. A urlencoded, trimmed form got HTTP 500 on 2026-10-29. The reusable client
  successfully submitted two signed test PDFs on 2026-10-29; response DRN,
  IRNs and signature status matched the confirmation email, which also
  confirmed type 331000 for each. Other response shapes remain unverified.
- Signature validation is pyHanko, not a marker scan: `intact` (byte range
  digest), `valid` (CMS signature), and full-file coverage are checked
  separately. No PDF trust roots are configured; signer trust is unverified.
- `www.icegate.gov.in` omits its issuing intermediate. The pinned
  `Entrust DV TLS Issuing RSA CA 2` in `certs/` fixes the chain.
- Browser-free login worked without an OTP challenge in live tests. The user
  requested removal of OTP handling; stop with an error if login is rejected.

Filing attempts can only be repeated with `--allow-duplicate` after reconciliation;
the override can create a duplicate filing. The confirmation challenge remains required.

## Testing

```bash
uv sync
uv run pytest
```

Tests use scripted fake transports. They must not touch the network, the real
the real repository `.env`, or the user's browser profile.

Before any live run:

```bash
uv run icegate doctor --env-file <path>
uv run icegate login --env-file <path>
uv run icegate esanchit handoff --env-file <path>
```

## Live staging runs

A live upload requires **explicit user authorization** and a chosen,
already-signed PDF. Run exactly one file first:

```bash
uv run icegate esanchit stage <file> --doc-type <code> --dump-html /tmp/opencode/icegate-client/page.html
```

The `--doc-type` value is cross-checked against that row's option list and
printed as a plan; it is not saved by the portal during staging. Verify the
printed rows, the `planned type (not saved)` column, and the saved HTML. Never
run `stage` twice "to see if it worked": the second run adds more rows to the
same session.

## Live submit runs

Submission is irreversible and needs its own authorization, every time.

```bash
uv run icegate esanchit submit <file> --doc-type <code> --dry-run      # plan + challenge
uv run icegate esanchit submit <file> --doc-type <code> --confirm 'SUBMIT 1 <plan_id>'
```

Before the run: confirm the file is the right one, the SHA-256 prefix matches,
and the document code is correct. After the run: read the receipt at
`<state-dir>/receipts/<plan_id>.json`. On exit 3 (`unknown`) **do not re-run**;
open the portal and look. Record the observed response shape in this file and in
the Smart Gmail portal notes when a new response shape is observed.

`--dump-html` writes the post-upload page to a mode-0600 file. It contains a
session-scoped `tabId`, so keep it out of the repository and delete it when
done.

## Sign and upload (standing authorization)

When the user says "sign and upload" files, do not ask for the token PIN.
Signing goes through the user's script, which keeps the PIN inside itself
(untracked, mode 700, outside this repo). Never print, echo, log or commit it.

1. Copy or move the files into `~/Downloads/Signer`.
2. Run `SIGN_ALL_IN_TERMINAL=1 ~/Downloads/Signer/sign-all.sh`. It signs every
   unsigned `.pdf`, `.sb`, `.be`, `.txt`, `.xml` there and writes `name[signed].ext`.
3. Upload only the `[signed]` files, each time as plan first, then confirm once:

```bash
uv run icegate filing upload --dry-run <signed .sb/.be>      # prints "challenge FILE N <id>"
uv run icegate filing upload --confirm 'FILE N <id>' <same>  # SB/BE filing
uv run icegate esanchit submit --dry-run --doc-type-map 'NAME=CODE' ... <signed pdfs>
uv run icegate esanchit submit --confirm 'SUBMIT N <id>' --doc-type-map ... <same>
```

If signing fails with `PinIncorrect`, stop: more tries can lock the token. On a
5xx or `unknown` outcome do not re-run; read `submit_error_body` in the run
journal and check mail for the confirmation. Report DRN/IRNs or the HTTP status
and receipt path; a filing 202 is not final, the filing-status email decides.

## Style

- Python 3.11+, type hints, `from __future__ import annotations`.
- No new runtime dependencies without a reason; the current set is `requests`,
  `cryptography`, `certifi`, `lxml`, `pyhanko`, `filelock`. `pyhanko` brings
  `aiohttp` and `asn1crypto` transitively; that is the price of real signature
  validation. Do not add `httpx`, `tenacity`, `typer` or `pydantic`.
- Parsing is lxml, not a hand-rolled tree. Never enable entity resolution or
  network access in the parser.
- Errors derive from `IcegateError` and carry no secrets.
- Keep the journal structured; add fields instead of free-form text.
