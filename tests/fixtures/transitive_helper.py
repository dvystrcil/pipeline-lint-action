"""Two levels of sync helpers, one a module-level function, plus psycopg2."""
import psycopg2


def _connect(dsn):
    return psycopg2.connect(dsn)


class Filter:
    def _ensure_schema(self):
        conn = _connect("dbname=x")
        conn.close()

    def _record(self, row):
        self._ensure_schema()

    async def outlet(self, body):
        self._record(body)  # EXPECT: self._record
        return body
