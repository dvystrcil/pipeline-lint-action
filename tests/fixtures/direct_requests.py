import requests


class Filter:
    async def inlet(self, body):
        requests.post("http://x", json=body)  # EXPECT: requests.post
        return body
