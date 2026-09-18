# Load test results

Run with `make load` against the stack `make up` brings up. Re-runnable; these are the numbers from
the run recorded here, not a target.

## Environment

**Read this before quoting any number below.** Every figure here belongs to the environment in
this table and to no other.

| | |
|---|---|
| Host | Apple Silicon laptop, Docker Desktop (Linux VM) |
| Topology | the real one: nginx → two app replicas (2 uvicorn workers each) → Postgres 16 / Redis 7, all containers |
| Measured from | a `grafana/k6` container on the compose network, addressing `web` by service name |
| Dataset | `seed_demo --seed 1`, giving 400 customers, 2,427 accounts, 13,011 entries, 27,228 journal lines |
| Postgres | `fsync=off` (the CI override), so write latency is optimistic |
| Throttles | **on**, at their shipped rates (ADR-0015) |

Two caveats that matter more than the rest. `fsync=off` makes the transfer figures better than a
durable box would give. And an M-series laptop is faster than a `t3.small`, which is burstable, so a
sustained run there would drain CPU credits and degrade, which is the single most interesting thing
this profile would find on real hardware and cannot find here.

## Profile

100 read VUs ramped over 2m50s plus 10 write VUs, each VU pinned to its own seeded customer and
paced inside that customer's own budget: reads at 2 rps against a 4 rps ceiling, writes at 0.33 rps
against 0.5. Read mix weighted toward the endpoints that derive: 40% `/accounts/`, 30%
`/portfolio/`, 20% `/holdings/`, 10% `/orders/`.

## Results

**21,585 requests at 125.3 rps, 0.00% failed, 0 throttled, and the ledger still balanced.**

| Endpoint | median | p90 | p95 | p99 | max |
|---|---|---|---|---|---|
| `/accounts/` (derived balances) | 5.4 ms | 15.8 ms | **23.6 ms** | 43.2 ms | 99.7 ms |
| `/holdings/` (derived + valued) | 5.6 ms | 16.6 ms | **24.4 ms** | 42.1 ms | 78.3 ms |
| `/orders/` (plain indexed read) | 5.6 ms | 16.3 ms | **26.3 ms** | 40.1 ms | 105.8 ms |
| `/portfolio/` (three aggregates) | 6.9 ms | 18.4 ms | **26.4 ms** | 42.9 ms | 79.4 ms |
| `POST /transfers/` (locked write) | 14.0 ms | 33.4 ms | **52.3 ms** | 81.2 ms | 126.9 ms |
| all | 6.0 ms | 17.7 ms | **25.6 ms** | 44.2 ms | 126.9 ms |

`check_ledger_invariants` passes after the run, covering zero-sum, share conservation, no negative
asset balance and no negative holding, across the ~570 transfers the write scenario posted.

The result worth reading is the **comparison between rows, not the rows themselves**: `/portfolio/`
derives cash, holdings, valuation and realized P&L on every request and lands within 5 ms of
`/orders/`, which is an indexed filter with a `select_related` and derives nothing. At this data
size, deriving balances on read costs almost nothing. That is a claim about 27,000 journal lines,
not a general one. The cost of a `SUM` grows with an account's lifetime line count, and one run at
one size cannot show where that stops being true.

## Re-run on the current code

The table above, and the three-topology comparison below it, were measured **before** the
proxy-header fix. That is not a footnote: at the time, `proxy_set_header` was not reaching any
proxied location, so nginx forwarded `Host: app` and none of `X-Forwarded-For`,
`X-Forwarded-Proto` or `X-Request-ID` arrived at all. Those numbers describe a system that no
longer exists.

Re-run on the current code, same profile, same dataset, same machine:

| Endpoint | median | p90 | p95 | p99 | max |
|---|---|---|---|---|---|
| `/accounts/` | 9.9 ms | 34.1 ms | **44.3 ms** | 69.1 ms | 217.6 ms |
| `/holdings/` | 10.0 ms | 35.3 ms | **46.0 ms** | 74.9 ms | 130.1 ms |
| `/orders/` | 10.9 ms | 38.2 ms | **48.4 ms** | 77.6 ms | 103.9 ms |
| `/portfolio/` | 11.9 ms | 39.2 ms | **50.3 ms** | 78.4 ms | 149.0 ms |
| `POST /transfers/` | 16.4 ms | 49.0 ms | **69.8 ms** | 101.7 ms | 134.1 ms |
| all | 10.8 ms | 36.5 ms | **47.4 ms** | 76.7 ms | 217.6 ms |

21,270 requests at 123.3 rps, 0.00% failed, 0 throttled, 0 conflicts, invariants hold.

**Slower than the run above, and the honest answer is that it is not known how much of that is the
code.** The shape is unchanged and the ordering across endpoints is unchanged, so nothing here
reads as a regression in kind. But a laptop measured hours apart is not a controlled comparison,
and attributing a 25 ms p95 becoming 47 ms to the forwarded headers, to thermal state, or to
anything else would be a guess. What can be said is that the earlier figures were taken through a
proxy configuration that was broken, and these were not. Prefer these.

## The three topologies, measured

Same profile, same dataset, same machine, run once before the connection pool, once after, and
once after the second app replica.

| | 1 replica, no pool | 1 replica, pooled | 2 replicas, pooled |
|---|---|---|---|
| failed | 5.35% | 0.00% | **0.00%** |
| median | 20.7 ms | 9.0 ms | **6.0 ms** |
| p95 | 108.5 ms | 48.9 ms | **25.6 ms** |
| p99 | 168.0 ms | 87.9 ms | **44.2 ms** |
| throughput | 120.1 rps | 123.3 rps | **125.3 rps** |
| peak Postgres connections | 100 (exhausted) | 26 | **33** |

Throughput barely moves because the harness paces itself. It is pinned near 123 rps by the rate
limits, not by the server. Latency is where the change shows: the pool halved it by removing
connection churn, and the second replica halved it again by doubling the worker count behind nginx.
The replicas exist for gapless releases (ADR-0043); the latency was a side effect.

The `t3.small` caveat gets sharper here, not softer. Four gunicorn workers plus two Celery forks on
2 burstable vCPUs is a different machine from the one these numbers came off.

## What the first run found

The first run of this harness failed, and the failure was the point.

**5.35% of requests returned 500.** Not throttling: `throttled_429` was zero throughout. Postgres
was refusing connections:

```
psycopg.OperationalError: connection failed: FATAL: sorry, too many clients already
```

Django's ASGI handler runs each sync view in its own thread, and a Django connection is
thread-local, so the connections one process holds are bounded by requests *in flight* rather than
by worker count, and `CONN_MAX_AGE = 60` then held each one open for a further minute. At 100
concurrent readers across two workers that went past Postgres's 100-connection ceiling. Nine weeks
of tests could not have found this: the suite runs one request at a time.

The fix was a psycopg 3 connection pool (`DB_POOL_MAX`, 20 per process), which bounds what a
process can hold and makes an overloaded app queue for a connection rather than fail to get one.
Raising `max_connections` would only have moved the cliff.

| | before | after |
|---|---|---|
| failed requests | 5.35% | **0.00%** |
| median | 20.7 ms | **9.0 ms** |
| p95 | 108.5 ms | **48.9 ms** |
| p99 | 168.0 ms | **87.9 ms** |
| peak Postgres connections | 100 (exhausted) | **26** |

Latency more than halved. Connection churn, not query cost, had been the dominant expense.

## Two harness bugs, also worth recording

Both produced failures that looked like findings and were not.

**Unfunded accounts.** The minter took each customer's first two spendable accounts without
checking balances, and the seed leaves some savings accounts empty, so part of the write budget went
on transfers correctly rejected for insufficient funds. It now requires both accounts funded.

**Idempotency keys repeating across runs.** Keys were `load-<vu>-<iteration>`, which collide between
runs against the same database: same key, different transfer, which is a 409 by design, because
ADR-0024 binds a key to a digest of its payload rather than letting it replay the wrong movement.
The harness was wrong and the ledger was right. Keys now carry a per-run nonce, and
`transfer_conflicts` is a threshold so a future recurrence fails the run instead of being absorbed
into the error rate.

## What this does not measure

- **Other hardware.** These describe the environment above. Re-run on the deployment target
  before quoting them there.
- **Sustained load.** Under three minutes, which is far too short to see `t3` credit exhaustion.
- ~~**Growth.**~~ Measured. See the growth curve below.
- **WebSockets.** HTTP only. `deploy/smoke_socket.py` covers socket delivery, but not under load.
- **The breaking point.** Every run stayed inside the rate limits on purpose, so this establishes
  the stack is comfortable at 123 rps, not where it stops being comfortable.


---

# The growth curve

Run with `make growth`. Answers the question the load profile above explicitly could not: balances
are derived rather than stored (ADR-0008), so what does that cost as an account accumulates
history?

`tests/test_query_counts.py` already proves the query *count* stays flat as accounts are added.
That rules out an N+1 and says nothing about what one of those queries costs. This measures the
cost.

## Fixture

`manage.py bench_derived_balance` opens one customer per depth and posts alternating transfers
between their own Checking and Savings until the account carries the target line count. Through
`ledger.services.transfer`, the real service, so every line went through the same overdraft check,
lock ordering and audit write the application uses.

Same host, same topology and same container-on-the-compose-network measurement as the load profile
above, so the two sets of numbers are comparable. Depths are the line count actually present, which
is why they are not round: each measurement pass adds its own timed transfers.

## Read latency through nginx

40 samples per cell after 8 discarded warm-ups, paced at 0.3s, which matches how `api.js` paces and
is what makes these comparable to the p95 figures above.

| lines on the account | `/accounts/` p50 | p95 | `/portfolio/` p50 | p95 |
|---|---|---|---|---|
| 400 | 19.5 ms | 22.4 ms | 22.3 ms | 24.9 ms |
| 1,300 | 20.2 ms | 23.0 ms | 22.9 ms | 25.6 ms |
| 10,300 | 24.6 ms | 27.7 ms | 26.7 ms | 29.1 ms |
| 100,300 | **60.2 ms** | 64.2 ms | **61.3 ms** | 63.5 ms |

For scale, the fixed floor on the same path: `/healthz` 1.4 ms (nginx alone), `/api/v1/ready/`
3.1 ms, `/api/v1/auth/me/` 4.8 ms (authentication, no aggregate).

**Flat to about a thousand lines, then linear.** A 250x increase in history costs about 3x in
latency, and the first 1,000 lines cost nothing measurable. That is the honest shape: the design is
comfortable for a realistic consumer account and degrades predictably rather than falling over.

## Where the time goes

`EXPLAIN (ANALYZE, BUFFERS)` on the balance aggregate alone, with warm buffers:

| lines | execution | shared buffers | plan |
|---|---|---|---|
| 300 | 0.08 ms | 26 | Index Scan |
| 1,200 | 0.22 ms | 73 | Bitmap Heap Scan |
| 10,200 | 1.57 ms | 502 | Bitmap Heap Scan |
| 100,200 | 14.34 ms | 4,804 | Bitmap Heap Scan |

Two things are visible here that the latency table cannot show.

**The planner switches strategy between 300 and 1,200 lines**, from an index scan to a bitmap heap
scan. Nothing is wrong with that, but it is why the curve has a knee rather than starting linear.

**Every row costs a heap fetch.** At 100k the plan reads `Heap Blocks: exact=2,279`, because
neither index on `ledger_journalline` carries `amount`. `line_account_created_idx` is
`(account, created_at)` and the implicit FK index is `(account_id)` alone. A covering index would
turn this into an index-only scan and cut the buffer count several-fold. It would not change the
complexity: summing N rows is O(N) whichever structure they are read from.

Note also that the warm 14.34 ms aggregate does not account for the full 40 ms the client sees at
that depth. The gap is cache residency: `EXPLAIN` here ran against buffers left hot by the previous
statement, while a paced client comes back to an account whose 2,279 heap blocks may have been
evicted. Measured back to back with no pacing, the same request settles at about 27 ms.

## The write path is on the same curve, and it is worse

`transfer` checks the overdraft by deriving the source balance (ADR-0010), so the same sum runs on
every posting. 100 timed transfers at each depth, with the channel layer dropped for the reason
below:

| lines on the account | per transfer | throughput |
|---|---|---|
| 300 | 2.05 ms | 488.5 /s |
| 1,200 | 2.03 ms | 493.7 /s |
| 10,200 | 3.47 ms | 288.3 /s |
| 100,200 | **24.90 ms** | **40.2 /s** |

**A 12x throughput loss**, against 3x on the read side. This is the more interesting half of the
result and the one the read curve alone would have hidden: a derived balance is usually discussed
as a read-side trade, and here it costs writes considerably more.

It is still far above what the stack is asked for. The `transfer` scope is 30/min per customer
(ADR-0015) and the load profile above moves about 3 transfers a second in total, so even the 100k
account has two orders of magnitude of headroom. The number worth keeping is the slope, not the
ceiling.

## What would fix it

Not an index. A covering index helps the constant and leaves the complexity alone.

The change that would matter is **balance checkpoints**: a periodic row per account recording the
balance as at a point in time, so a sum starts from the last checkpoint instead of from account
opening. The ledger is append-only and lines are never backdated (`AccountQuerySet.with_balance`
documents why an as-of sum is stable), which is exactly the property that makes a checkpoint safe
to trust.

It has not been built, and at this scale it should not be. A consumer account posting ten
transactions a month reaches 1,200 lines in a decade, which this table shows costs nothing. Adding
a denormalised balance now would introduce the one thing the whole design exists to prevent: a
second place where a balance is written, which can disagree with the ledger. The trigger for
building it is an account crossing roughly 10k lines in normal use, not a benchmark reaching it
deliberately.

## What this run found

**Sustained writes exhaust the channel layer's Redis connections.** Seeding drove ~170 transfers a
second from one process and logged 2,242 `ConnectionError: Error 99 ... Cannot assign requested
address` in the process. Every posting publishes a realtime event (ADR-0023); `async_to_sync`
builds a fresh event loop per call and `RedisChannelLayer` keys its connection pool on the running
loop, so each publish opens a connection and the ephemeral port range runs out.

**The ledger was unharmed, which is the part worth recording.** The publish is best-effort and
outside the transaction, so every write committed: the 10,000-line account finished with exactly
10,000 lines and invariants held. `tests/test_ws_rollback.py` asserts that property against a mock;
this is the first time it has been observed against a real Redis under real load.

Not fixed here, deliberately. Triggering it needs a sustained write rate two orders of magnitude
above what the shipped throttles permit, and the fix touches the realtime hot path, so it belongs
in its own change rather than riding along in a benchmark. The write measurements above drop the
channel layer so they price the overdraft check rather than the failure.

**`make load` was broken and nothing noticed.** `api.js` never set a `Host` header, so it reached
nginx as `Host: web` and Django answered 400 `DisallowedHost` to every request. That became true
when `DJANGO_ALLOWED_HOSTS` was tightened to `localhost,127.0.0.1`, and no CI job runs `make load`,
so the harness had been silently broken since. Both harnesses now send an accepted `Host` rather
than the list being widened, because keeping that list minimal is precisely what surfaced the
missing proxy headers in the first place.

## What this does not measure

- **Concurrency at depth.** Sequential by design: this varies one thing, and adding queueing would
  mix in a second. What a 100k-line account does to p95 under 100 concurrent readers is a
  different run.
- **Other hardware**, for the same reason as the profile above.
- **Anything but cash.** The benchmark customers hold no positions, so `/portfolio/` here exercises
  the cash aggregate rather than valuation across many instruments.
