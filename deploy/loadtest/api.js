// Load profile for the deployed stack: derived-balance reads, plus the locked transfer write.
//
// Run it with `make load`, which mints the tokens this needs and then starts this script in a
// container on the compose network.
//
// ── What is worth measuring here, and why ──────────────────────────────────────────────────────
//
// Balances are not stored. `/accounts/` sums an account's signed journal lines and `/portfolio/`
// derives cash, holdings and valuation the same way (ADR-0008, ADR-0020). That is a deliberate
// design choice with a cost that only shows up under load, so it is the thing this profile leans
// on: the reads are weighted toward the endpoints that aggregate, not toward the cheap ones.
//
// The seeded dataset is ~27,000 journal lines over ~2,400 accounts, so these sums are real work
// against `line_account_created_idx` — but note what that means for the number. This measures
// derived reads at *this* data size, where the average account holds a few dozen lines. How the
// cost grows with an account's lifetime line count is a different question, and one run at one
// size cannot answer it. `make growth` does: it seeds single accounts to 100, 1k, 10k and 100k
// lines and measures the same endpoints against each. See the growth-curve section of RESULTS.md,
// and read it before quoting the p95 below as if it were size-independent.
//
// ── Staying inside the rate limits ─────────────────────────────────────────────────────────────
//
// The throttles stay on (ADR-0015). A load test that disables the limits it ships with measures a
// system nobody runs, and "we turned the limits off first" is the sentence that invalidates the
// number. Instead the harness fans out across every seeded customer and paces each one under its
// own budget:
//
//   user scope     240/min  = 4.0 rps per customer   → reads sleep 0.5s, so 2 rps. Half the ceiling.
//   transfer scope  30/min  = 0.5 rps per customer   → writes sleep 3s,  so 0.33 rps.
//
// A POST to /transfers/ is charged against *both* scopes — `DEFAULT_THROTTLE_CLASSES` runs the user
// throttle on every view and `ScopedRateThrottle` adds the scoped one on top — so the two pools are
// kept disjoint below rather than reasoned about jointly.
//
// `throttled_429` is a threshold, not a statistic: a single 429 means the harness outran a budget
// and the latency figures describe a partially-rejected workload. The run fails rather than
// reporting a number that would have to be explained.

import http from 'k6/http';
import exec from 'k6/execution';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';

const fleet = JSON.parse(open('/loadtest/tokens.json'));

// Through nginx, by service name on the compose network — not against gunicorn directly. The
// number has to describe what a client experiences, which includes the proxy hop, the keepalive
// pool and the gzip pass.
const BASE = __ENV.BASE || 'http://web';

// **The connection target and the Host header are two different things, and both matter.** BASE
// reaches nginx by compose service name so the measurement excludes Docker Desktop's userland port
// forward. But that makes the request's Host `web`, and DJANGO_ALLOWED_HOSTS deliberately lists
// only `localhost` and `127.0.0.1` — nginx forwards `Host: $host` straight through, so Django
// answers 400 DisallowedHost to every request.
//
// Setting it here rather than widening ALLOWED_HOSTS is the whole point: the list is minimal on
// purpose, because a name in it that matches an nginx service or upstream is what masked the
// missing proxy headers for nine weeks (see deploy/nginx/proxy-headers.conf). A real browser sends
// the site's own domain; this sends the name Django is configured to answer to.
const HOST_HEADER = __ENV.HOST_HEADER || 'localhost';

// Unique per run, supplied by `make load`. Without it the idempotency keys below repeat between
// runs against the same database, and the second run collides with the first: same key, different
// transfer, which is a 409 by design (ADR-0024 binds a key to a digest of its payload rather than
// letting it replay the wrong movement). That is the ledger being right and the harness being
// wrong, and it cost a run to work out.
const RUN_ID = __ENV.RUN_ID || `${Date.now()}`;

// Disjoint customer pools, so a read VU and a write VU never share a throttle bucket.
const WRITE_POOL = Math.min(20, Math.floor(fleet.length / 4));
const READ_POOL = fleet.length - WRITE_POOL;

const throttled = new Counter('throttled_429');
const conflicts = new Counter('transfer_conflicts');

export const options = {
    scenarios: {
        reads: {
            executor: 'ramping-vus',
            exec: 'readProfile',
            startVUs: 0,
            stages: [
                { duration: '20s', target: 40 },
                { duration: '60s', target: 40 },
                { duration: '20s', target: 100 },
                { duration: '60s', target: 100 },
                { duration: '10s', target: 0 },
            ],
            gracefulRampDown: '10s',
        },
        writes: {
            executor: 'constant-vus',
            exec: 'writeProfile',
            vus: 10,
            duration: '170s',
        },
    },
    thresholds: {
        // Any non-2xx is a failure worth stopping for. There is no expected error in this profile.
        http_req_failed: ['rate<0.01'],
        // Not a service level anyone promised — a tripwire, set well above the observed figures so
        // it catches a regression rather than ratifying whatever the machine did today.
        http_req_duration: ['p(95)<1000'],
        'http_req_duration{endpoint:portfolio}': ['p(95)<1000'],
        'http_req_duration{endpoint:accounts}': ['p(95)<1000'],
        'http_req_duration{endpoint:holdings}': ['p(95)<1000'],
        'http_req_duration{endpoint:orders}': ['p(95)<1000'],
        'http_req_duration{endpoint:transfer}': ['p(95)<1500'],
        // The claim this whole profile rests on.
        throttled_429: ['count==0'],
        // A conflict means a key was reused for a different payload — always a harness bug, never
        // a finding about the system. Fail rather than absorb it into the error rate.
        transfer_conflicts: ['count==0'],
    },
    summaryTrendStats: ['min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

function headersFor(customer) {
    return {
        headers: {
            Host: HOST_HEADER,
            Authorization: `Bearer ${customer.access}`,
            'Content-Type': 'application/json',
        },
    };
}

function record(response, endpoint) {
    if (response.status === 429) {
        throttled.add(1);
    }
    check(response, { [`${endpoint} is 2xx`]: (r) => r.status >= 200 && r.status < 300 });
}

// Weighted toward the endpoints that aggregate. `/orders/` is here as the control: it is a plain
// indexed filter with a `select_related`, so it is what "no derivation" costs on the same stack.
const READ_MIX = [
    { weight: 40, path: '/api/v1/accounts/', endpoint: 'accounts' },
    { weight: 30, path: '/api/v1/portfolio/', endpoint: 'portfolio' },
    { weight: 20, path: '/api/v1/holdings/', endpoint: 'holdings' },
    { weight: 10, path: '/api/v1/orders/', endpoint: 'orders' },
];

export function readProfile() {
    const customer = fleet[exec.vu.idInTest % READ_POOL];

    let roll = Math.random() * 100;
    let choice = READ_MIX[READ_MIX.length - 1];
    for (const candidate of READ_MIX) {
        if (roll < candidate.weight) {
            choice = candidate;
            break;
        }
        roll -= candidate.weight;
    }

    const response = http.get(`${BASE}${choice.path}`, {
        ...headersFor(customer),
        tags: { endpoint: choice.endpoint },
    });
    record(response, choice.endpoint);

    // 2 rps against a 4 rps budget. See the header.
    sleep(0.5);
}

export function writeProfile() {
    const customer = fleet[READ_POOL + (exec.vu.idInTest % WRITE_POOL)];
    const [first, second] = customer.accounts;

    // Alternate direction so a long run does not drain one side and start failing the overdraft
    // check — which would turn a latency measurement into a measurement of rejections.
    const forward = exec.scenario.iterationInInstance % 2 === 0;

    const body = JSON.stringify({
        source_account: forward ? first : second,
        destination_account: forward ? second : first,
        amount: '1.00',
        description: 'loadtest',
        // Unique per request: this profile measures the posting path, not the replay path. A
        // repeated key would return the original entry without touching the ledger, and the
        // numbers would quietly describe a cache hit.
        idempotency_key: `load-${RUN_ID}-${exec.vu.idInTest}-${exec.scenario.iterationInInstance}`,
    });

    const response = http.post(`${BASE}/api/v1/transfers/`, body, {
        ...headersFor(customer),
        tags: { endpoint: 'transfer' },
    });

    if (response.status === 409) {
        conflicts.add(1);
    }
    record(response, 'transfer');

    // 0.33 rps against a 0.5 rps budget. See the header.
    sleep(3);
}
