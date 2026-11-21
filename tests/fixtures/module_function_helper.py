import time


def backoff():
    time.sleep(3)


async def outlet(body):
    backoff()  # EXPECT: backoff
    return body
