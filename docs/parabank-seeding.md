# Seeding ParaBank test data

Reference for building deterministic fixtures against a local ParaBank
instance, so discovery and replay runs execute against known state.

Assumes `PARABANK_BASE_URL=http://localhost:8080/parabank` (adjust to your
port). All REST calls live under `$PARABANK_BASE_URL/services/bank`.
Swagger UI for your instance: `$PARABANK_BASE_URL/api-docs/index.html`.

**Send `Accept: application/json`** — the service returns XML by default.

---

## 1. Database lifecycle

| Purpose | Call |
|---|---|
| Restore the shipped seed data (`john`/`demo` and its accounts) | `POST /services/bank/initializeDB` |
| Drop all data | `POST /services/bank/cleanDB` |

These are the same two buttons as `admin.htm`. Call `initializeDB` at the
start of a seed run to get a fixed baseline; call it again before a replay
run to make assertions on extracted outputs exact and reruns idempotent.

```bash
curl -sS -X POST "$PARABANK_BASE_URL/services/bank/initializeDB" -H 'Accept: application/json'
```

## 2. Creating a customer — no REST endpoint

The REST surface has no create-customer operation. Registration is a plain
Spring MVC form POST with **no CSRF token**, so `requests` (or curl) can
drive it directly.

```
POST $PARABANK_BASE_URL/register.htm
Content-Type: application/x-www-form-urlencoded
```

Field names, verbatim:

```
customer.firstName
customer.lastName
customer.address.street
customer.address.city
customer.address.state
customer.address.zipCode
customer.phoneNumber
customer.ssn
customer.username
customer.password
repeatedPassword
```

A successful POST returns 200 with a "Your account was created
successfully. You are now logged in." panel; a duplicate username returns
200 with `This username already exists.` in the error span — check the body,
not the status code.

Use synthetic values only. Real SSNs/addresses are exactly the regulated
data `safety/redact.py` exists to keep out of `evidence/`.

## 3. Resolving the customer id

```
GET /services/bank/login/{username}/{password}
```

Returns the customer object including `id`, `firstName`, `lastName`,
`address`, `ssn`. Every account-level call below needs that `id`.

Note this endpoint puts the password in the URL path — it will land in any
proxy or access log. Fine for a local fixture script, worth never using
from the agent loop itself.

## 4. Accounts

```
POST /services/bank/createAccount?customerId={id}&newAccountType={type}&fromAccountId={id}
```

`newAccountType`: `0` = CHECKING, `1` = SAVINGS.
`fromAccountId` funds the new account from an existing one; the opening
balance is the "Initial account balance" application parameter (see §6).

Read back with:

```
GET /services/bank/customers/{customerId}/accounts
GET /services/bank/accounts/{accountId}
```

## 5. Manufacturing transactions

These are how you produce transaction rows with amounts you choose, which
is what a `find-transactions`-style capability needs to assert against.

```
POST /services/bank/deposit?accountId={id}&amount={n}
POST /services/bank/withdraw?accountId={id}&amount={n}
POST /services/bank/transfer?fromAccountId={a}&toAccountId={b}&amount={n}
```

Query endpoints for verifying what you seeded:

```
GET /services/bank/accounts/{accountId}/transactions
GET /services/bank/accounts/{accountId}/transactions/amount/{amount}
GET /services/bank/accounts/{accountId}/transactions/onDate/{onDate}
GET /services/bank/accounts/{accountId}/transactions/fromDate/{from}/toDate/{to}
GET /services/bank/accounts/{accountId}/transactions/month/{month}/type/{type}
GET /services/bank/transactions/{transactionId}
```

Also available: `POST /services/bank/requestLoan?customerId=&amount=&downPayment=&fromAccountId=`,
and stock positions via `POST /services/bank/customers/{id}/buyPosition`
and `/sellPosition` (`GET /services/bank/customers/{id}/positions`).

`POST /services/bank/customers/update/{customerId}` takes the same field
set as registration, as query params.

## 6. Tuning app behaviour

```
POST /services/bank/setParameter/{name}/{value}
```

Mirrors the Application Settings on `admin.htm`: initial account balance,
minimum required balance, loan provider, loan processor, threshold. Also
`POST /services/bank/shutdownJmsListener` / `/startupJmsListener` if you
want to test behaviour with the loan JMS path down.

---

## Manufacturing error states

The brief's hard requirement is that replay handles runtime errors, not just
the happy path. Most of them can be produced on demand rather than waited
for:

| Error state | How to force it |
|---|---|
| Validation error | Transfer more than the account balance, or withdraw below the minimum required balance |
| Record not found | Run a capability against an account id, then `cleanDB` |
| Empty result set | Seed an account with no transactions above the filter threshold |
| Permission-ish / session expiry | Drive the UI, then drop the `JSESSIONID` cookie mid-flow |
| Transient slowness / app error | `setParameter` to an invalid value, or stop the HyperSQL listener |

## Known limits

- **Transaction dates are always "today."** `deposit`/`withdraw`/`transfer`
  stamp the current date, so date-range fixtures can't be built through
  REST. For those, write directly to the HyperSQL DB (default port 9001)
  or set the container clock.
- **Bill pay is UI-only.** It has no REST operation, so a bill-pay
  capability needs its payee created by driving `billpay.htm`.
- **Registration is UI-only**, as above.

## Repo wiring

- `config/allowlist.yaml` lists `parabank.parasoft.com` under
  `allowed_domains`. Add `localhost` (and `127.0.0.1`) before pointing the
  agent at a local instance, or every action gets rejected.
- `PARABANK_BASE_URL` in `.env` needs to move to the local URL.
- Seeding is fixture setup, not automation under test: the seed script may
  use the REST API freely, but discovery and replay must still go through
  the UI surface — that is the thing being evaluated.

## Sources

- Endpoint list derived from `ParaBankService.java` in
  https://github.com/parasoft/parabank
- Registration field names read off the live register form at
  https://parabank.parasoft.com/parabank/register.htm
