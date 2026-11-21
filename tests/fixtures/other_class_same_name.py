"""self.helper resolves within the enclosing class only."""
import requests


class Other:
    def _helper(self):
        return requests.get("http://x")


class Filter:
    def _helper(self):
        return 1

    async def inlet(self, body):
        self._helper()
        return body
