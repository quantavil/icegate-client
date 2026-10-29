# Electronic filing research

Observed on the authenticated ICEGATE File Upload page on 2026-10-29.

- The Better ICEGATE shortcut points to
  `#/role-dashboard/services/file-upload`. It is separate from the eSANCHIT
  upload page and the Shipping Bill e-copy page.
- The page accepts multiple files and says it allows up to 10. Its displayed
  extension list includes `.sb`, `.be` and `.json`, but not `.txt`; do not
  rename or convert a file without checking its actual format and signing needs.
- The page offers Sign using the ICEGATE local DSC signing service and says
  already signed files can be uploaded. The required signed SB/BE file format,
  signature verification and any compatibility with `icegate-sign` are not
  established.
- The public page JavaScript invokes `userService.uploadFile` once per file;
  that method constructs multipart form data with field `messageFile` and
  posts to `fileServiceUrl + "file"`. The production configuration contains
  `https://foservices.icegate.gov.in/filing/`, suggesting the endpoint
  `POST /filing/file`. The service sets `channel: dashboard`, while the global
  interceptor sets `channel: browser` and adds session authentication headers.
  One separate browser-free test using dashboard channel received HTTP 202 and
  `validationStatus: SUCCESS` with a message saying Customs is processing it.
- The official [ICEGATE API list](https://www.icegate.gov.in/advisories/integrate-with-icegate-on-api%27s)
  lists separate BE/SB JSON API documentation and schemas. The
  [March 2026 API contract](https://www.icegate.gov.in/sites/default/files/2026-03/Advisory%20Contract%20Document%20for%20BE%20and%20SB%20Open%20API%20Filing%20%281%29_0.pdf)
  describes certificate-based credential encryption and multipart submission.
  Do not assume that JSON API accepts legacy SB/TXT files or that its
  authentication is interchangeable with the dashboard token.

## Authorized test, 2026-10-29

The user supplied `61826_signed_.sb` and requested an upload test, expecting a
subsequent error. One direct POST returned HTTP 202 and an initial success
message. A later email from `no-replyregistration@icegate.gov.in` identified
control 61826 and reported **DSC validation failed**. Initial upload acceptance
does not mean the filing passed DSC validation or was accepted by Customs.
No automatic retry was performed; a private attempt record is kept under the
client's state directory.

The file contains CACHE01 text and signature/certificate sections. The separate
[`icegate-sign` text verifier](https://github.com/quantavil/icegate-sign/blob/main/src/icegate_sign/text.py)
reported its signature invalid; the precise reason for the DSC failure remains
unverified. Do not treat the signature markers alone as proof of validity.

For the test file, ICEGATE's status email matched the message receiver, type,
control number and filing date embedded in the filing. Those values do not
uniquely identify an upload attempt: multiple messages may share the same
tuple and the email has no file hash. A separate read-only Gmail workflow may
search within a bounded time window and verify the sender and full message,
but must treat conflicting or indistinguishable candidates as ambiguous. Only
failure messages were observed; a successful status template is unverified.

The reusable CLI was tested on 2026-10-29 with one deliberate repeat of the
same test file, using a separate state directory to preserve the earlier
attempt record. It received HTTP 202 and `accepted_for_processing`. An email
arriving at the same time reported header validation failure for the same
control number and filing details; because the email has no attempt ID or
file hash, it cannot be conclusively attributed to this upload. The module
checks only that SB/BE signature sections exist and does not verify their
cryptography. A file upload can be a real customs filing: do not repeat an
ambiguous request. Duplicate protection is local to the selected state directory.
