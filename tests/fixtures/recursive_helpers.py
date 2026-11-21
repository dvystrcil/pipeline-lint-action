"""Mutually recursive sync helpers must not hang the lint."""
import requests


class Filter:
    def _a(self, n):
        if n:
            return self._b(n - 1)
        return requests.get("http://x")

    def _b(self, n):
        return self._a(n)

    def _c(self, n):
        return self._c(n - 1) if n else 0

    async def inlet(self, body):
        self._b(3)  # EXPECT: self._b
        self._c(3)
        return body
