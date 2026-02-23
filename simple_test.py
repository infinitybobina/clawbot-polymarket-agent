simple_test.py
print("🔥 TEST START")

import os
print("1. os:", "OK")

key = os.getenv("OPENAI_API_KEY")
print("2. Key:", "✅" if key else "❌")

try:
    import openai
    print("3. openai:", "OK")
    print("🎉 ALL GOOD - LLM готов!")
except:
    print("4. openai: FAIL")

print("🔥 TEST END")