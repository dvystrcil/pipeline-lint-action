import time
import urllib.request
from urllib.request import urlopen as uo
import requests as rq


async def inlet(body):
    time.sleep(1)  # EXPECT: time.sleep
    urllib.request.urlopen("http://x")  # EXPECT: urllib.request.urlopen
    uo("http://x")  # EXPECT: urllib.request.urlopen
    rq.get("http://x")  # EXPECT: requests.get
    return body
