"""The open-webui#67 gap: async def -> plain def helper -> requests.get."""
import requests


class Filter:
    def _fetch_note_text(self, url):
        return requests.get(url, timeout=10).text

    async def inlet(self, body):
        body["note"] = self._fetch_note_text("http://x")  # EXPECT: self._fetch_note_text
        return body
