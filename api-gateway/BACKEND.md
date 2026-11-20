# api-gateway backend extension guide

Runtime: **nestjs**. Role: **gateway**. Shared implementation: `src/shared/`.

The generated model/schema, service, and controller/router are wired automatically.
For services, add business rules in the generated module service's before/after
create, update, and delete hooks. Keep transport code in its controller/router.
For gateways, a generated router file owns each service prefix and delegates to
the shared proxy. Existing JWT/API-key authorization remains in the request path.

Model definitions supplied through `--models=...` produce schemas, validation,
OpenAPI, frontend forms and routes during generation. Changing a schema manually
after generation does not automatically regenerate the other artifacts. Use a
fresh output directory to review a regenerated project; this is not an in-place
migration tool for developer code.

The shared model facade provides find/findOne/findById, countDocuments, distinct,
aggregate, create/insertMany, updateOne/updateMany/updateById and delete operations
(using snake_case in Python and exported Go method names). See
`shared-contract.json` for the exact supported operators. Unsupported queries
fail explicitly. This is a portable, bounded subset, not arbitrary Mongoose
compatibility. Supported scalar filters, sorting, paging and counts run in SQL or
MongoDB. Common numeric groups also run in the database. A positive native subset
can narrow candidates before a residual predicate; pagination follows the residual.
Cassandra uses positive ID constraints for bounded partition reads, with general
queries evaluated over a bounded scan. `explain` reports the planner path, not an
optimizer plan or an index-use guarantee.

QUERY_MAX_ROWS (default 1000, maximum 10000) and QUERY_MAX_BYTES (default 8 MiB,
maximum 64 MiB) bound returned/candidate rows and fallback scans. A residual limit
never permits scanning unlimited rows. Expansion and relationship results are
bounded too. `findPage` uses ID keyset pagination; totals are separate reads.
Declare `x-huza-indexes` in model schemas for startup-created indexes. Portable
string indexes need maxLength 1–191; Cassandra supports only the existing id key.
`x-huza-relations` declares same-service scalar-key joins. Server-side `populate`
requires the target Model and an authorization callback returning its allowed read
filter (null/nil/None denies). It is deliberately absent from public query routes.

Updates use single-record compare-and-swap and retry conflicts at most 16 times.
`upsertById(id, operators, onInsert)` combines insert-if-absent with these updates;
it never silently upserts another unique key. Python uses `upsert_by_id`, Go
`UpsertByID`. Request retries are not exactly-once processing. Ordered bulk writes
are individually durable, not multi-record atomic. Before hooks may repeat on a
conflict; keep them pure. After hooks run after commit, and their failure cannot
undo the write. Use the native driver for database-specific transactions and
unsupported queries; native writes bypass facade validation and concurrency rules.
DB_POOL_SIZE defaults to 4 (1 for SQLite), with a maximum of 32 per replica.

Redis response caching runs in the generated model HTTP controllers, after live
JWT/API-key authorization. GET list/detail and POST _query/_aggregate use separate
credential/route/query keys. Shared Store writes change the model cache version
after commit, before after-hooks; a conditional Redis fill prevents a slow read
from restoring an older version. Successful bulk operations invalidate per write.
Read-only POSTs, failed writes and facade no-op updates do not invalidate.

Generate with --cache=none to omit Redis, or set CACHE_ENABLED=false at runtime.
CACHE_TTL_SECONDS defaults to 30 (1–300); CACHE_MAX_BYTES defaults to 1 MiB
(1 KiB–8 MiB). X-Cache reports HIT/MISS/BYPASS. Cache-Control: no-cache/no-store,
X-Cache-Bypass: 1, or ?cache=miss bypass reads and fills. CACHE_VARY_HEADERS adds
request headers to the key and gateway forwarding; authorization, X-API-Key,
Accept-Language and Origin always partition keys. Do not include volatile request
IDs. Custom request-dependent policies must include their context headers or bypass.

Redis is optional acceleration: reads/writes continue if it is unavailable, with
short client timeouts. DB commit and Redis invalidation are not one transaction.
A crash/outage between them can leave old data until the entry's original TTL;
concurrent in-flight reads can observe pre-write data. Use bypass for reads needing
freshness, or disable caching for strict-consistency workloads. Native driver
writes must explicitly invalidate the model through store.cache.invalidate(model)
(Node/Python/Java), or store.Cache.Invalidate(model) (Go), after commit. Custom
controllers and joined responses are not automatically cached; if you add caching,
you must invalidate every model whose response depends on the changed data.
Do not use cache hits to skip required audit events or other read side effects.

Shared messaging clients provide confirmed Kafka/RabbitMQ sends and serial
consumers, retrying handlers before an optional dead-letter publication. Handlers
must be idempotent because a crash after processing but before acknowledgement
can cause redelivery. Configure `--messaging=kafka|rabbitmq|both` when generating
the project to provision development brokers. Default `none` starts no brokers.
Prepare the RabbitMQ topic/group binding before publishing; unroutable sends fail.
CRUD hooks do not publish automatically. Database writes and message publication
are not one transaction. Supply an outbox when business rules need that guarantee.
Clients accept KAFKA_BROKERS, KAFKA_TLS, KAFKA_CA_FILE, KAFKA_USERNAME/PASSWORD,
and RABBITMQ_URL (amqps supported by native clients). Bundled Kafka is internal,
single-node plaintext development infrastructure; configure your secured broker
for production. RabbitMQ uses generated credentials in the project's .env.

Requests carry a validated X-Request-ID and structured request logs without
request bodies or authorization headers. RATE_LIMIT_PER_MINUTE is an optional
process-local limit (default disabled), not a distributed quota. Health checks
are exempt. Configure REQUEST_MAX_BYTES for request limits and REQUEST_LOGGING
for request logging; credentials and live authorization decisions are not cached.
