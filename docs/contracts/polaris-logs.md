# The Polaris log records: contract v3

What the log sidecar and the main Fluent Bit hand to the observability team's OpenSearch
(ADR-081, which supersedes ADR-036 and amends ADR-024): which lines are kept, where they go,
what a record holds, and what the aggregate holds. A change to it is a new version, announced to
the observability team before it ships (ADR-024's rule, carried over by ADR-081).

**Status.** The values below are placeholders where marked and **assumed, not measured on the
syslog route**: whether Polaris's syslog JSON carries `hostName`, `sequence`, `mdc` and
`loggerName` like the file appender's is not yet known, and the proof on the gate amends this
file if they differ. The lines of the examples were measured on 2026-10-11 from Polaris 1.6.0
where the example says so, and constructed from them otherwise (`"source"` in the routing file).
Every Polaris upgrade re-checks this file (ADR-009).

The machine-readable form is ten files next to this one, all named `polaris-logs-v3-*`:

| File | What |
|---|---|
| `polaris-logs-v3-routing.json` | The rule table (equal to ADR-081's, checked by `scripts/test_polaris_logs_contract.py`), the derived counters, the names that are gone, and the examples: a Polaris line and the record it becomes |
| `polaris-logs-v3-candidate-line.schema.json`, `polaris-logs-v3-error-line.schema.json` | JSON Schema (2020-12) of a record of each index |
| `polaris-logs-v3-aggregate.schema.json` | JSON Schema of an aggregate document |
| `polaris-logs-v3-candidate-template.json`, `polaris-logs-v3-error-template.json`, `polaris-logs-v3-aggregate-template.json` | The three index templates |
| `polaris-logs-v3-candidate-ism.json`, `polaris-logs-v3-error-ism.json` | The two ISM policies |
| `polaris-logs-v3-transform.json` | The transform from the two line indices to the aggregate |

## Three indices (names are placeholders)

| Index | Holds | Kept |
|---|---|---|
| `polaris-logs-candidate-*` (daily) | Lines routed to `candidate` | 3 to 4 days: ISM deletes the index at `min_index_age: 3d`, and the index is daily |
| `polaris-logs-error-*` (daily) | Lines routed to `error` | 30 to 31 days: ISM deletes the index at `min_index_age: 30d`, and the index is daily |
| `polaris-logs-aggregate` | One document per group and hour | The observability team's normal retention; no ISM policy here |

An error line is in the error index only, never also in the candidate index. Both line
templates carry the alias `polaris-logs-source`, which is the transform's source: the pattern
`polaris-logs-*` would match the aggregate index and feed it back. The templates set
`dynamic: false` and map every property of their schema; `message` is `text` with a keyword
subfield, `exception` is an object that is not indexed. Shards and replicas (1 and 1) are
placeholders. Only `mdc.requestId` and `mdc.realmId` are indexed; any other `mdc` key is kept in the document and not searchable. Install order: the templates and the ISM policies before the first document, the aggregate template before the transform starts, or the target gets dynamic mappings. That OpenSearch 3.6.0 accepts the templates, the policies and the transform is
not yet verified.

## Which lines go where: policy-v5

The sidecar reads every line, redacts, and routes it by the first rule that matches. The rules
are the audit policy v5 of contract v2 without its counters, hold or state (ADR-081
Decision 3):

| # | A line that is | Goes to |
|---|---|---|
| * | any line whose message holds an unmasked `clientSecret` | The value becomes `<redacted>` and `secret_redacted: true` is added, before any rule |
| 1 | level `ERROR` or `WARN` | error |
| 2a | from `LocalIcebergCatalog` (commit timing) | dropped |
| 2b | from `IcebergExceptionMapper` or `PolarisServiceImpl`, not caught by rule 1 | candidate (placeholder: ADR-081 named no index) |
| 2c | from any other application logger | dropped |
| 3' | an access line with status 404 | candidate |
| 3 | an access line with status 400 or above except 404, or one that does not parse | error |
| 4 | an access line for PUT, DELETE or PATCH | candidate |
| 5 | an access line for a POST under `/api/management/` | candidate |
| - | an access line for any other POST | dropped |
| 6 | an access line for a successful GET or HEAD | dropped |
| 7 | anything else | error (placeholder: an unexpected line is worth keeping longer) |

First match wins, so a failed PUT, DELETE, PATCH or POST is an error line (rule 3), never a
candidate line. Dropped lines are counted nowhere. Rule 2b is stateless: v2's hold for the
request's access line is gone, so there is no `held_orphan`. The mechanism that sends a record
to its index (a tag, or two streams) is not part of this contract; the field `index_class`
carries the result in either case.

## A record

A JSON object per line, UTF-8. A reader accepts fields it does not know. The Polaris fields are
as in v2; `loggerClassName`, `ndc`, `processName` and `processId` are removed, and so is the
syslog envelope (application name, host, process id) once the body is parsed.

| Field | Type | In | What |
|---|---|---|---|
| `timestamp`, `loggerName`, `level`, `message`, `hostName` | string | both, required | Polaris's own; `message` is redacted. `hostName` is assumed on the syslog route |
| `sequence`, `threadName`, `threadId`, `mdc` | | both, optional | Polaris's own; optional because the syslog route is not measured |
| `exception` | object | both, optional | Polaris's JSON `exception`, in the shape contract v2 records |
| `client_ip`, `http_method`, `api_path`, `http_status`, `response_size`, `access_log_parse_error` | | access lines | As in v2, parsed by the sidecar |
| `user_principal_name` | string | both, required | The caller on an access line (`-` for none); `-` on any other line |
| `secret_redacted` | `true` | both | As in v2 |
| `event_id` | string, no whitespace | both, required | The document id, so a resend overwrites. Placeholder composition: `<hostName>:<timestamp>:<sequence>`, unique per pod incarnation (the SHA-1 of v2 has no Lua builtin). It is unique only while `sequence` is present: a record without one (the syslog route is not measured) can collide, and the later overwrites the earlier |
| `index_class` | `candidate` or `error` | both, required | The index the record belongs to (the candidate schema accepts only the first, the error schema only the second) |
| `policy_rule` | string | both, required | The rule id above: `2b`, `3'`, `4`, `5` in the candidate index; `1`, `3`, `7` in the error index |
| `policy` | `policy-v5` | both, required | |
| `resource`, `resource_kind`, `api_kind` | string | both, required | The 21 resource patterns of v2 (`/tables/t/metrics` counts on `/tables/t`), their kind, and `catalog`, `management` or `other`. A line that is not an access line carries `-`, `none`, `none` |
| `status_class` | string | both, required | `ok` (below 400), `401`, `403`, `404`, `4xx` (other 4xx), `5xx`, `unparsed` (an access line that did not parse), `none` (not an access line) |

Gone from v2's line: `log_hour`, `held_orphan` and the SHA-1 `event_id`.

Example: a request without a token, rule 3, error index (measured 2026-10-11, re-dated;
wrapped here).

```json
{"timestamp":"2026-10-30T12:40:11.753600155Z","sequence":1771,
 "loggerName":"io.quarkus.http.access-log","level":"INFO",
 "message":"172.17.0.1 - - [30/Oct/2026:12:40:00 +0000] \"GET /api/management/v1/catalogs HTTP/1.1\" 401 -",
 "hostName":"polaris-7f4d69c67c-aaaaa","client_ip":"172.17.0.1","user_principal_name":"-",
 "http_method":"GET","api_path":"/api/management/v1/catalogs","http_status":401,
 "response_size":0,"event_id":"polaris-7f4d69c67c-aaaaa:2026-10-30T12:40:11.753600155Z:1771",
 "index_class":"error","policy_rule":"3","policy":"policy-v5",
 "resource":"/api/management/v1/catalogs","resource_kind":"collection","api_kind":"management",
 "status_class":"401"}
```

## The aggregate

The transform runs continuously over the alias `polaris-logs-source` (the candidate and the
error index) and writes one document per group to `polaris-logs-aggregate`. The groups are
`window_start` (the UTC hour), `hostName`, `index_class`, `resource`, `resource_kind`,
`api_kind`, `user_principal_name`, `status_class` and `level`; `index_class` is a group so that
two transforms, the fallback if one source over two indices is refused (one per index kind, the
same target), never write the same bucket. The metrics:

| Field | What |
|---|---|
| `lines` | The count of kept lines (`value_count` of `event_id`) |
| `response_bytes` | The sum of `response_size` |
| `min_record_time`, `max_record_time` | The first and the last `timestamp` |

A document has no other field. There is no cap on rows (v2's `__other__` is gone): the aggregate holds one document per distinct combination of the nine groups, so many principals, resources or pods make it grow towards one document per line. A non-access line carries `-` or `none` in the access group
fields so that no group lacks one (whether the transform would skip such a document is not
known). The shortest interval of the transform (placeholder: 1 minute), the bucket `_id` and the
format of the time fields (string or epoch milliseconds) are not known either.

**Counters are sums over `status_class`**, so a reader derives them; they are not fields:

| Counter | Sum of `lines` where `status_class` is | Replaces |
|---|---|---|
| `not_found` | `404` (from the candidate index) | `counted_404` |
| `errors_4xx` | `401`, `403` or `4xx`; **404 is not in it** | v2's `errors_4xx`, which counted it |
| `auth_denied` | `401` or `403` | `auth_denied` |
| `errors_5xx` | `5xx` | `errors_5xx` |
| `errors_kept` | any, where `index_class` is `error` | `errors_kept` |
| `by_pod` | any, grouped by `hostName` | `by_pod` |

**Gone, with no replacement** (ADR-081 Decision 6): `lines_in`, `processed`, `dropped`,
`malformed`, `sources`, `corrupt_files`, `orphans_moved`, `idle_log_files`, `pod_list_error`,
`access_seen`, `access_kept`, `access_counted`, `counted_read`, `counted_post`, `counted_404`,
`parse_errors`, `bytes_total`, `distinct_resources`, `distinct_principals`, `resources_other`,
`resources_other_distinct`, `principals_other`, `role_keys_forced`, `app_dropped*`,
`held_orphans`, `commit_*`, the row caps (`__other__`, `__errors__`) and the check that the
books balance. Reads, 2a and 2c lines and other dropped lines are not counted anywhere.

## Delivery loss

The record goes from Polaris to the sidecar over a syslog stream with an asynchronous queue
that discards when full (ADR-081 Decision 2), and from the sidecar to OpenSearch through the
main Fluent Bit. A line can therefore be lost while the sidecar is down or slow, and nothing
counts it: the aggregate is the count of lines that arrived, not of lines Polaris wrote.

---

# Contract v2 (superseded by v3, ADR-081)

**Superseded.** Contract v3 above replaces this one. It stays in force, unchanged, for
`charts/polaris-log-batch`, `images/polaris-log-batch` and the gate until they are removed;
its five schema files (`polaris-logs-processed.schema.json`, `-summary`, `-resource`,
`-principal` and `-app-dropped.schema.json`) are not edited.

What the log batch hands to the observability team's Fluent Bit (ADR-024): where the files
are, what each line holds, and when an hour is complete. The decisions are ADR-014, ADR-024
and ADR-036; this file is the format they call a contract. A change to it is a new version,
announced to the observability team before it ships (ADR-024).

Lines measured on 2026-10-11 from Polaris 1.6.0 (the image pinned in
`charts/polaris/values-lakehouse.yaml`) with the file appender this repository sets. Every
Polaris upgrade re-checks this file (ADR-009).

The machine-readable form is five JSON Schemas (2020-12) next to this file: one for a
processed line, `polaris-logs-processed.schema.json`, and one per row type of the aggregated
file, named by its `report_type`: `polaris-logs-summary`, `-resource`, `-principal` and
`-app-dropped.schema.json`. The smoke gate checks every line the stand-in Fluent Bit reads
against them (`gates/contract.py`).

## Where

The claim `polaris-logs` (`ReadWriteMany`) in `NS_CATALOG_POLARIS`. The observability team's
reader mounts it in that namespace and reads two directories:

| Path | What |
|---|---|
| `processed-logs/YYYYMMDD-HH.jsonl` | The lines of that UTC hour the audit policy keeps, one JSON object per line |
| `aggregated-logs/YYYYMMDD-HH.jsonl` | A summary row for that hour, then its resource, principal and app_dropped rows. **Written last: once it exists, the hour is complete** |

Everything else on the claim is not part of the contract: Polaris's own files
(`polaris-<pod>.log` and their hourly `.gz` rolls), and the batch's `malformed/`, `done/`,
`.tmp/` and `.state/`.

- **Hours are UTC.** `YYYYMMDD-HH` names the hour `[HH:00, HH+1:00)`.
- **Each file appears whole, by a rename.** A reader never sees a half-written file.
- **Every hour gets a file, even an empty one.** A `processed-logs/` file may hold no line.
- **Files are deleted 3 days after publication**, so the reader picks an hour up within 3 days.
- **The batch runs at minute 3 of every hour.** An hour is normally complete a few minutes
  after it ends; after an outage, the batch catches up to 72 hours per run, oldest first.

## Which lines are kept: the audit policy

The batch reads every line of the hour, then keeps some and counts the rest; nothing is dropped
without a number in the aggregated file. The policy is the archive's policy v5
(`enjay27/lakehouse-k8s` at `09a4773`), the batch port of the Fluent Bit Lua filter it ran
before, and gives the same lines and counters as that filter (checked again on 2026-10-11 with
the archive's `logging/scripts/step16-batch-lua-parity.py`), with one change: Polaris 1.6.0 logs
commits from `org.apache.polaris.service.catalog.iceberg.LocalIcebergCatalog`, where the
archive's Polaris had `IcebergCatalog` (measured on 2026-10-11). The first rule that matches
wins:

| # | A line that is | Kept? |
|---|---|---|
| * | any line whose message holds an unmasked `clientSecret` | The value becomes `<redacted>` and `secret_redacted: true` is added, before any rule |
| 1 | level `ERROR` or `WARN` | **Kept** |
| 2a | from `LocalIcebergCatalog`: `Successfully committed to table/view ... in N ms` | Its time is counted on the table's or view's row, then 2c |
| 2b | from `IcebergExceptionMapper` or `PolarisServiceImpl` | Decided by its request's access line (below) |
| 2c | from any other application logger | Dropped, counted per logger (`app_dropped`) |
| 3' | an access line with status 404 | Dropped, counted (`counted_404`) |
| 3 | an access line with status 400 or above, or one that does not parse | **Kept** |
| 4 | an access line for PUT, DELETE or PATCH | **Kept** |
| 5 | an access line for a POST under `/api/management/` | **Kept** |
|   | an access line for any other POST | Dropped, counted (`counted_post`) |
| 6 | an access line for a successful GET or HEAD | Dropped, counted (`counted_read`) |
| 7 | anything else | **Kept** |

So every refused request (401, 403) and every change to principals, roles and grants is kept.

**A request's application lines (2b).** They are decided by the access line with the same
`mdc.requestId`, the nearest one at or after them within 30 s, else the nearest before them,
across the hour's boundary: a 404 drops them too (`app_dropped_404`); any other status keeps
them; with no access line within 30 s they are kept with `held_orphan: true`
(`held_orphans`); with no request ID they are kept.

## A processed line

The JSON object Polaris wrote, without four fields, plus what the batch adds. UTF-8, one
object per line.

| Field | Type | From | What |
|---|---|---|---|
| `timestamp` | string | Polaris | When it was logged, UTC, up to nanoseconds: `2026-10-11T12:40:47.753600155Z` |
| `sequence` | integer | Polaris | The log record's sequence number in its pod |
| `loggerName` | string | Polaris | The category, e.g. `io.quarkus.http.access-log` |
| `level` | string | Polaris | `TRACE`, `DEBUG`, `INFO`, `WARN`, `ERROR` or `FATAL` |
| `message` | string | Polaris | The message, its client secret redacted |
| `threadName` | string | Polaris | The thread |
| `threadId` | integer | Polaris | The thread's id |
| `mdc` | object | Polaris | `requestId` and `realmId` while a request is served; `{}` otherwise |
| `hostName` | string | Polaris | The pod's name |
| `client_ip`, `user_principal_name`, `http_method`, `api_path` | string | the batch | On an access line: parsed from its message. `user_principal_name` is `-` for a request without a principal |
| `http_status`, `response_size` | integer | the batch | On an access line; `response_size` is 0 where the log has `-` |
| `access_log_parse_error` | `true` | the batch | On an access line whose message does not parse |
| `secret_redacted` | `true` | the batch | When a client secret was redacted |
| `held_orphan` | `true` | the batch | Rule 2b found no access line for the request |
| `event_id` | string | the batch | SHA-1, 40 hex digits, of the line's raw bytes as Polaris wrote them: the same line always gets the same id, so a reader can use it as the document id |
| `log_hour` | string | the batch | The hour the line was published in, `YYYYMMDD-HH` |

Polaris's `loggerClassName`, `ndc`, `processName` and `processId` are removed. Other fields may
appear, and a reader must accept fields it does not know. One is known: `exception`, which
Polaris's JSON format adds when a log record carries one. Measured on 2026-10-11: a request
Polaris 1.6.0 answered 503 wrote one such line, `IcebergExceptionMapper`'s `Full
RuntimeException` at `INFO`, which rule 2b keeps; one it answered 500 (its S3 endpoint did not
answer) wrote `Unhandled exception returning INTERNAL_SERVER_ERROR` at `ERROR`, which rule 1
keeps (the Create Fixtures artifact, ADR-040); its 400, 404 and 409 answers wrote none. Its
shape:

| Field | Type | What |
|---|---|---|
| `exceptionType` | string | The Java class, e.g. `org.apache.iceberg.exceptions.RuntimeIOException` |
| `message` | string | The exception's message |
| `frames` | array | The stack, each `{"class", "method", "line"}`: 65 frames on the line measured |
| `causedBy` | object | `{"exception": {...}}`, the cause in the same shape, when there is one |
| `suppressed` | array | The suppressed exceptions in the same shape, when there are some: the S3 client's failed attempts on the 500 |
| `refId` | integer | The exception's number within the record, from 1 |

Such a line is long: about 15 KB on the 503, 40 KB on the 500 with five suppressed.

**Every HTTP request** Polaris serves on its API port writes one access line, whose `message`
is the access log line (`client - principal [time] "METHOD path HTTP/1.1" status bytes`) and
whose `mdc.requestId` is the caller's `X-Request-ID`, or one Polaris generates when the caller
sent none. A request refused for want of a token is logged the same way, with principal `-`
and bytes `-` (measured on 2026-10-11). The management port's health probes are not logged.
Request bodies, tokens and client secrets are not in the lines.

Example (wrapped here): a request without a token, kept by rule 3.

```json
{"timestamp":"2026-10-11T12:40:48.283104402Z","sequence":1771,
 "loggerName":"io.quarkus.http.access-log","level":"INFO",
 "message":"172.17.0.1 - - [11/Oct/2026:12:40:48 +0000] \"GET /api/management/v1/catalogs HTTP/1.1\" 401 -",
 "threadName":"vert.x-eventloop-thread-3","threadId":27,
 "mdc":{"requestId":"gate-req-0002","realmId":"POLARIS"},
 "hostName":"polaris-7f4d69c67c-aaaaa","client_ip":"172.17.0.1","user_principal_name":"-",
 "http_method":"GET","api_path":"/api/management/v1/catalogs","http_status":401,
 "response_size":0,"event_id":"…40 hex digits…","log_hour":"20261011-12"}
```

## The aggregated file

The summary row first, then resource rows, principal rows and app_dropped rows, each sorted by
its key. Every row starts with the same fields:

| Field | Type | What |
|---|---|---|
| `contract_version` | `2` | This contract's version |
| `report_type` | string | `summary`, `resource`, `principal` or `app_dropped` |
| `window_start`, `window_end` | string | The hour's bounds, `+00:00` |
| `window_seconds` | `3600` | The hour's length |

### The summary row

| Field | Type | What |
|---|---|---|
| `hour` | string | `YYYYMMDD-HH` |
| `policy` | `"policy-v5"` | The audit policy that kept the lines |
| `lines_in` | integer | Lines read for the hour: `processed + dropped + malformed` |
| `processed`, `dropped` | integer | Lines kept in the hour's `processed-logs/` file, and lines the policy counted instead |
| `malformed` | integer | Lines that were not a timestamped JSON object, kept out of the hand-off |
| `pods` | array of strings | Pods with a line in the hour |
| `by_pod` | object | Lines per pod |
| `sources` | array | The files read: `file`, `lines` |
| `corrupt_files` | array | Rolls that could not be decoded: `file`, `error` |
| `orphans_moved` | array or null | Files of pods that are gone, moved aside after this hour: `file`, `pod`, `last_line_hour`, `unterminated_tail`. `null` when the pod list could not be read |
| `idle_log_files` | array or null | Files of live pods whose last line is older than the hour |
| `pod_list_error` | string or null | Why the pod list could not be read |
| `access_seen`, `access_kept`, `access_counted` | integer | Access lines read, kept, and counted instead: `access_kept = access_seen - access_counted` |
| `counted_read`, `counted_post`, `counted_404` | integer | Access lines counted by rule 6, rule 5 and rule 3' |
| `errors_kept`, `parse_errors` | integer | Access lines kept by rule 3, and those that did not parse |
| `errors_4xx`, `errors_5xx`, `auth_denied`, `bytes_total` | integer | Over every access line: statuses 400-499 (401 and 403 also in `auth_denied`), 500 and above, and response bytes |
| `distinct_resources`, `distinct_principals` | integer | Resource rows with a request, principal rows |
| `resources_other`, `resources_other_distinct`, `principals_other`, `role_keys_forced` | integer | What the row caps did (below) |
| `app_dropped_total`, `app_dropped_404`, `held_orphans` | integer | Application lines dropped by rule 2c, dropped with their 404, kept without an access line |
| `min_record_time`, `max_record_time` | string | The first and last access line's `timestamp`; absent in an hour without one |
| `message` | string | The counters in one sentence, for people |
| `published_at` | string | When the batch published it, `+00:00` |

An hour whose counts do not add up (`lines_in`, `dropped = access_counted + app_dropped_total
+ app_dropped_404`) is not published: the batch fails and the next run tries again.

### A resource row

One per resource, keyed by `resource`: the path without its query, cut after the first of 21
patterns that matches (`/tables/t/metrics` counts on `/tables/t`; a grant
`/catalog-roles/cr/grants` on the role `cr`). A commit `cat.a.b.t` counts on
`/api/catalog/v1/cat/namespaces/a%1Fb/tables/t`.

| Field | Type | What |
|---|---|---|
| `resource` | string | The key |
| `resource_kind` | string | `table`, `view`, `namespace`, `catalog`, `principal`, `principal-role`, `catalog-role`, `collection`, `auth`, `config`, `transaction`, `other`; `error` for `__errors__` |
| `api_kind` | string | `catalog`, `management`, `other`; `mixed` for `__errors__` and `__other__` |
| `requests`, `reads`, `writes` | integer | Access lines, GET/HEAD, POST/PUT/DELETE/PATCH |
| `errors`, `errors_4xx`, `errors_5xx`, `auth_denied` | integer | Statuses 400 and above, split as in the summary |
| `response_bytes` | integer | Response bytes |
| `last_read_bytes`, `last_write_bytes` | integer | The size of the last successful read or write, when there was one |
| `commit_count`, `commit_ms_sum`, `commit_ms_min`, `commit_ms_max` | integer | The commits Polaris logged, when there were some |

**Caps.** At most 500 resource rows; the rest count on `__other__`. A failed request makes no
row of its own unless its resource succeeded or committed somewhere in the hour: it counts on
`__errors__`, so a client knocking on names that do not exist cannot fill the rows. Failed
requests on roles may still make up to 100 rows (`role_keys_forced`), since a refused grant is
what a role's row is there to show.

### A principal row

One per caller, keyed by `user_principal_name` (`-` for a request without one), at most 200
(the rest count on `__other__`): the same counters as a resource row, without
`last_*_bytes`.

### An app_dropped row

One per application logger whose lines rule 2c dropped, at most 50 (the rest count on
`__other__`): `logger_name` and `dropped`. A logger that appears here for the first time is
worth a look.

## Versions

- **v1** (PR #49): Polaris's lines as written, every one of them, and the summary row. Shipped
  in no release.
- **v2** (this file, #45): the audit policy decides which lines are kept, the report rows join
  the summary. The first contract a release ships. Corrected before any release shipped it
  (#54): rule 2a names Polaris 1.6.0's `LocalIcebergCatalog`, so commits reach the rows instead
  of only the `app_dropped` counts; the `exception` field's shape is recorded.
- **v3** (ADR-081): Polaris logs through a sidecar into two line indices (candidate, 3 days;
  error, 30 days) and an aggregate made by a transform; two line shapes, the routing of policy-v5
  per rule, 404 lines kept in the candidate index, the batch's counters replaced by sums over
  `status_class`. v2 stays in force for the batch until it is removed.
