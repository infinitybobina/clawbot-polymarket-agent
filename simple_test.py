print("TEST START")

import os
print("1. os:", "OK")

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass

key = os.getenv("OPENAI_API_KEY")
print("2. Key:", "OK" if key else "MISSING")

try:
    import openai
    print("3. openai:", "OK")
    print("ALL GOOD - LLM ready")
except:
    print("4. openai: FAIL")

print("TEST END")