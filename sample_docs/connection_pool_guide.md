# Database Connection Pool Troubleshooting

This guide covers diagnosing and resolving connection pool exhaustion in the
payments service.

## Symptoms

The application logs show repeated `ECONNREFUSED` errors when connecting to the
upstream database, often followed by `pool exhausted` warnings. HTTP 503
responses are returned to clients while the pool is saturated.

## Root Cause

Connection pool exhaustion happens when connections are checked out faster than
they are returned. The most common causes are long-running transactions, a pool
size set too low for peak load, and connections leaked by code paths that fail
to release them in a `finally` block.

### Diagnosing

Check the current pool utilization:

```sql
SELECT count(*), state FROM pg_stat_activity GROUP BY state;
```

A large number of `idle in transaction` connections indicates leaked
connections held by long transactions.

## Resolution

| Step | Action |
| ---- | ------ |
| 1 | Increase `max_connections` to match peak concurrency |
| 2 | Set a statement timeout to kill stuck transactions |
| 3 | Audit code paths for missing connection release |

After applying the changes, restart the service and monitor the pool for 15
minutes to confirm the `ECONNREFUSED` errors have cleared.
