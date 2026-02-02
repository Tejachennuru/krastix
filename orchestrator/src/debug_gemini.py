
import os
import asyncio
import traceback
from langchain_google_genai import ChatGoogleGenerativeAI

async def test():
    print("--- DIAGNOSTIC START ---")
    key = os.getenv('GEMINI_API_KEY')
    if not key:
        print("❌ CRITICAL: GEMINI_API_KEY is NOT set in environment variables.")
        return

    print(f"🔑 Key loaded: {key[:5]}...{key[-5:]}")
    
    # Test 1: Standard Model
    print("\n[Test 1] Testing gemini-2.0-flash...")
    try:
        llm = ChatGoogleGenerativeAI(model="gemini-2.0-flash", google_api_key=key)
        res = await llm.ainvoke("Hello, simply reply with passed.")
        print(f"✅ Success! Response: {res.content}")
    except Exception:
        print(f"❌ Failed: {traceback.format_exc()}")

    # Test 3: Stable Pro Model
    print("\n[Test 3] Testing gemini-pro...")
    try:
        llm = ChatGoogleGenerativeAI(model="gemini-pro", google_api_key=key)
        res = await llm.ainvoke("Hello, simply reply with passed.")
        print(f"✅ Success! Response: {res.content}")
    except Exception:
        print(f"❌ Failed: {traceback.format_exc()}")

    # Test 4: 1.5 Pro
    print("\n[Test 4] Testing gemini-1.5-pro...")
    try:
        llm = ChatGoogleGenerativeAI(model="gemini-1.5-pro", google_api_key=key)
        res = await llm.ainvoke("Hello, simply reply with passed.")
        print(f"✅ Success! Response: {res.content}")
    except Exception:
        print(f"❌ Failed: {traceback.format_exc()}")
        
    print("--- DIAGNOSTIC END ---")

if __name__ == "__main__":
    asyncio.run(test())
