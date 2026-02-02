import asyncio
import os
import socket
import asyncpg
from urllib.parse import urlparse

# Force set the URL from what we know is in .env (for testing if env fetch fails)
# But better to read from os to test the actual container env
db_url = os.getenv("DATABASE_URL")
print(f"Testing Connection to: {db_url}")

async def test():
    try:
        parsed = urlparse(db_url)
        hostname = parsed.hostname
        print(f"Hostname: {hostname}")
        
        try:
            ip = socket.gethostbyname(hostname)
            print(f"Resolved IP: {ip}")
        except Exception as e:
            print(f"DNS Resolution Failed: {e}")
            
        print("Attempting asyncpg connect...")
        conn = await asyncpg.connect(db_url)
        print("SUCCESS! Connected.")
        await conn.close()
    except Exception as e:
        print(f"CONNECTION FAILED: {e}")

asyncio.run(test())
