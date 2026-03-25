test_api.py
print("Start")

import os
print("OS OK")

key = os.getenv("OPENAI_API_KEY")
print("Key:", "OK" if key else "NO")

from openai import OpenAI
print("OpenAI OK")

print("Finish")
