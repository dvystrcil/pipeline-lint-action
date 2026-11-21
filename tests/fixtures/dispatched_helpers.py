"""The correct fix pattern: every blocking helper runs off the event loop.
Nothing here may be flagged."""
import asyncio
import functools

import psycopg2
import requests


def _module_helper(url):
    return requests.get(url).text


class Filter:
    def _fetch(self, url):
        return requests.get(url, timeout=10).text

    def _connect(self):
        return psycopg2.connect("dbname=x")

    def _pure(self, x):
        return x + 1

    async def inlet(self, body):
        loop = asyncio.get_running_loop()
        a = await asyncio.to_thread(self._fetch, "http://x")
        b = await asyncio.to_thread(_module_helper, "http://x")
        c = await loop.run_in_executor(None, self._connect)
        d = await loop.run_in_executor(None, lambda: self._fetch("http://x"))
        e = await asyncio.to_thread(functools.partial(self._fetch, "http://x"))
        f = await asyncio.to_thread(lambda: _module_helper("http://x"))
        g = self._pure(1)  # non-blocking helper: fine
        return body
