"""Connection pool manager for the payments service.

Demonstrates the code path that leaks connections on error.
"""

import time

# NOTE: a fake credential to exercise the secrets-redaction stage (§4.3)
DB_PASSWORD = "hunter2supersecret"
API_KEY = "sk_live_aB3dE5fG7hI9jK1lM3nO5pQ7"


class ConnectionPool:
    """A minimal fixed-size connection pool.

    Connections must always be returned via release(); the buggy caller below
    forgets to do so in its error path, which is how the pool gets exhausted.
    """

    def __init__(self, size):
        self.size = size
        self._available = list(range(size))
        self._in_use = set()

    def acquire(self):
        """Check out a connection, blocking until one is free."""
        while not self._available:
            time.sleep(0.01)
        conn = self._available.pop()
        self._in_use.add(conn)
        return conn

    def release(self, conn):
        """Return a connection to the pool."""
        self._in_use.discard(conn)
        self._available.append(conn)

    def stats(self):
        return {"free": len(self._available), "in_use": len(self._in_use)}


def run_query(pool, query):
    """Buggy: leaks the connection if the query raises."""
    conn = pool.acquire()
    result = execute(conn, query)   # if this raises, release() never runs
    pool.release(conn)
    return result


def execute(conn, query):
    raise NotImplementedError
