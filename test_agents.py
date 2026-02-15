"""
Krastix Agent Test Script
=========================
Run AFTER: docker compose up --build
Tests the Research Agent and Form Agent with simple requests.

Usage:
    pip install httpx
    python test_agents.py
"""

import httpx
import time
import sys

BASE_ORCHESTRATOR = "http://localhost:8000"
BASE_RESEARCH = "http://localhost:8001"

TEST_USER_ID = "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
TEST_SESSION_ID = "b1eebc99-9c0b-4ef8-bb6d-6bb9bd380a22"


def header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def test_health_checks():
    """Test 1: Are all services alive?"""
    header("TEST 1: Health Checks")

    services = {
        "Orchestrator": f"{BASE_ORCHESTRATOR}/health",
        "Research Agent": f"{BASE_RESEARCH}/health",
    }

    all_ok = True
    for name, url in services.items():
        try:
            r = httpx.get(url, timeout=10)
            status = r.json()
            print(f"  [OK]   {name}: {status}")
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")
            all_ok = False

    return all_ok


def test_research_agent_direct():
    """Test 2: Hit the Research Agent directly (no DB needed)"""
    header("TEST 2: Research Agent — Direct HTTP (GENERAL_SEARCH)")

    payload = {
        "user_id": TEST_USER_ID,
        "task_type": "GENERAL_SEARCH",
        "query_or_url": "latest AI agent frameworks 2026",
        "context_metadata": {"task_id": "test-research-001"}
    }

    try:
        r = httpx.post(f"{BASE_RESEARCH}/research/run", json=payload, timeout=15)
        print(f"  Status Code: {r.status_code}")
        print(f"  Response:    {r.json()}")

        if r.status_code == 200 and r.json().get("status") == "accepted":
            print("  [PASS] Research agent accepted the task")
            return True
        else:
            print("  [FAIL] Unexpected response")
            return False
    except Exception as e:
        print(f"  [FAIL] {e}")
        return False


def test_research_agent_scrape():
    """Test 3: Research Agent — QUICK_SCRAPE a known URL"""
    header("TEST 3: Research Agent — Direct HTTP (QUICK_SCRAPE)")

    payload = {
        "user_id": TEST_USER_ID,
        "task_type": "QUICK_SCRAPE",
        "query_or_url": "https://example.com",
        "context_metadata": {"task_id": "test-research-002"}
    }

    try:
        r = httpx.post(f"{BASE_RESEARCH}/research/run", json=payload, timeout=15)
        print(f"  Status Code: {r.status_code}")
        print(f"  Response:    {r.json()}")

        if r.status_code == 200 and r.json().get("status") == "accepted":
            print("  [PASS] Scrape task accepted")
            return True
        else:
            print("  [FAIL] Unexpected response")
            return False
    except Exception as e:
        print(f"  [FAIL] {e}")
        return False


def test_orchestrator_chat_research():
    """Test 4: Hit orchestrator chat — should trigger research delegation"""
    header("TEST 4: Orchestrator Chat → Research Delegation")

    payload = {
        "user_id": TEST_USER_ID,
        "domain": "HR_RECRUITER",
        "message": "Search the web for top React developers in Singapore",
        "session_id": TEST_SESSION_ID
    }

    try:
        r = httpx.post(
            f"{BASE_ORCHESTRATOR}/api/v1/chat",
            json=payload,
            timeout=60  # LLM can be slow
        )
        print(f"  Status Code: {r.status_code}")
        resp = r.json()
        print(f"  Response:    {resp.get('response', str(resp))[:300]}...")

        if r.status_code == 200:
            print("  [PASS] Orchestrator responded")
            if resp.get("task_id"):
                print(f"  [INFO] Task delegated: {resp['task_id']}")
            return True
        else:
            print(f"  [FAIL] Status {r.status_code}: {resp}")
            return False
    except Exception as e:
        print(f"  [FAIL] {e}")
        return False


def test_orchestrator_chat_form():
    """Test 5: Hit orchestrator chat — should trigger form agent"""
    header("TEST 5: Orchestrator Chat → Form Delegation")

    payload = {
        "user_id": TEST_USER_ID,
        "domain": "HR_RECRUITER",
        "message": "Create a candidate application form for Senior Backend Engineer role with fields: name, email, resume upload, years of experience",
        "session_id": TEST_SESSION_ID
    }

    try:
        r = httpx.post(
            f"{BASE_ORCHESTRATOR}/api/v1/chat",
            json=payload,
            timeout=60
        )
        print(f"  Status Code: {r.status_code}")
        resp = r.json()
        print(f"  Response:    {resp.get('response', str(resp))[:300]}...")

        if r.status_code == 200:
            print("  [PASS] Orchestrator responded")
            if resp.get("task_id"):
                print(f"  [INFO] Task delegated: {resp['task_id']}")
            return True
        else:
            print(f"  [FAIL] Status {r.status_code}: {resp}")
            return False
    except Exception as e:
        print(f"  [FAIL] {e}")
        return False


if __name__ == "__main__":
    print("\n  KRASTIX AGENT TEST SUITE")
    print("  Ensure 'docker compose up --build' is running\n")

    results = {}

    # Always test health first
    results["Health Checks"] = test_health_checks()

    if not results["Health Checks"]:
        print("\n  [ABORT] Services not running. Start with: docker compose up --build")
        sys.exit(1)

    results["Research: GENERAL_SEARCH"] = test_research_agent_direct()
    results["Research: QUICK_SCRAPE"] = test_research_agent_scrape()
    results["Orchestrator → Research"] = test_orchestrator_chat_research()
    results["Orchestrator → Form"] = test_orchestrator_chat_form()

    # Summary
    header("RESULTS SUMMARY")
    for name, passed in results.items():
        icon = "[PASS]" if passed else "[FAIL]"
        print(f"  {icon} {name}")

    failed = sum(1 for v in results.values() if not v)
    print(f"\n  {len(results) - failed}/{len(results)} passed")
