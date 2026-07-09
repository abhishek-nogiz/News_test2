"""
Standalone diagnostic — verify HuggingFace embedding credentials/config,
independent of your pipeline code.

Run this in a Jupyter cell (or `python verify_hf_embeddings.py`) from your
project root, where a `.env` file with these two vars lives:

    HUGGINGFACE_API_KEY
    HUGGINGFACE_EMBEDDING_MODEL

It does 3 checks, each isolating a different possible failure point:
    A. Env vars are present and non-empty (not just "set to empty string")
    B. Raw HF Inference API call succeeds with those exact values
    C. Your own `create_embeddings_client` / `HuggingFaceEmbeddings` code
       succeeds (this isolates "my code has a bug" vs "my key/model is bad")

Read the output top to bottom — the first check that fails tells you
exactly where the problem is.
"""

import os
import sys
import traceback
from pathlib import Path

# ── Load .env exactly like your pipeline does ──────────────────────────
try:
    from dotenv import load_dotenv
    # Adjust this path if your notebook isn't at project root
    env_path = Path(".env")
    loaded = load_dotenv(env_path, override=True)
    print(f"[.env] load_dotenv found file: {env_path.resolve()} -> loaded={loaded}")
except ImportError:
    print("[.env] python-dotenv not installed — relying on already-exported env vars")

print()
print("=" * 70)
print("CHECK A — Environment variables")
print("=" * 70)

api_key = os.getenv("HUGGINGFACE_API_KEY", "")
model_name = os.getenv("HUGGINGFACE_EMBEDDING_MODEL", "")

print(f"HUGGINGFACE_API_KEY:        {'SET (len=' + str(len(api_key)) + ', prefix=' + api_key[:6] + '...)' if api_key else 'MISSING / EMPTY'}")
print(f"HUGGINGFACE_EMBEDDING_MODEL: {model_name if model_name else 'MISSING / EMPTY'}")

if not api_key:
    print("\n>>> STOP: HUGGINGFACE_API_KEY is empty. Fix your .env or env vars first.")
    sys.exit(1)
if not model_name:
    print("\n>>> WARNING: HUGGINGFACE_EMBEDDING_MODEL is empty — check B below will fail without a model name.")

print()
print("=" * 70)
print("CHECK B — Raw HuggingFace Inference API call (bypasses your code)")
print("=" * 70)

try:
    import requests
except ImportError:
    print(">>> STOP: `requests` not installed. pip install requests")
    sys.exit(1)

test_text = "James Rodriguez is a football player."

# HF Inference API — feature-extraction endpoint (standard embeddings route)
url = f"https://api-inference.huggingface.co/models/{model_name}"
headers = {"Authorization": f"Bearer {api_key}"}
payload = {"inputs": test_text, "options": {"wait_for_model": True}}

try:
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    print(f"HTTP status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        # Response shape varies by model type (sentence-embedding vs token-level)
        try:
            if isinstance(data, list) and data and isinstance(data[0], (int, float)):
                dim = len(data)
            elif isinstance(data, list) and data and isinstance(data[0], list):
                # token-level -> flatten first vector to check dim
                dim = len(data[0]) if isinstance(data[0][0], (int, float)) else len(data[0][0])
            else:
                dim = "unknown shape"
            print(f"SUCCESS — got embedding, dim={dim}")
        except Exception:
            print(f"SUCCESS (200) but couldn't parse shape. Raw type: {type(data)}, sample: {str(data)[:200]}")
    elif resp.status_code == 401:
        print(">>> AUTH FAILURE — HUGGINGFACE_API_KEY is invalid or expired.")
        print(f"    Body: {resp.text[:300]}")
    elif resp.status_code == 404:
        print(f">>> MODEL NOT FOUND — '{model_name}' doesn't exist or isn't accessible with this key.")
        print(f"    Body: {resp.text[:300]}")
    elif resp.status_code == 503:
        print(">>> MODEL LOADING — HF is cold-starting the model. Retry in ~20s (wait_for_model should handle this, but sometimes needs a second try).")
        print(f"    Body: {resp.text[:300]}")
    else:
        print(f">>> UNEXPECTED STATUS — investigate.")
        print(f"    Body: {resp.text[:300]}")
except requests.exceptions.RequestException as exc:
    print(f">>> NETWORK/REQUEST FAILURE: {exc!r}")
    print("    Check internet access, firewall, or HF API outage.")

print()
print("=" * 70)
print("CHECK C — Your project's create_embeddings_client() code")
print("=" * 70)

# Adjust PROJECT_ROOT if your notebook lives elsewhere relative to the repo
PROJECT_ROOT = Path(".").resolve()
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from news_agent.core.config import AppConfig
    from news_agent.services.internalLink.embeddings import create_embeddings_client
except Exception as exc:
    print(f">>> IMPORT FAILED: {exc!r}")
    print("    Adjust PROJECT_ROOT above, or check the module path matches your repo.")
    traceback.print_exc()
    sys.exit(1)

try:
    config = AppConfig.from_env()
    client = create_embeddings_client(config)
    if client is None:
        print(">>> create_embeddings_client() returned None.")
        print("    This means your code's own internal check decided not to build a client")
        print("    (e.g. it checks a different env var name than HUGGINGFACE_API_KEY —")
        print("    open embeddings.py and confirm the exact os.getenv(...) key it reads).")
    else:
        print(f"Client created: {type(client)}")
        embedding = client.embed(test_text)
        if embedding:
            print(f"SUCCESS — embed() returned vector of length {len(embedding)}")
        else:
            print(">>> embed() returned empty/falsy result — check embeddings.py's error handling,")
            print("    it may be swallowing an exception internally too.")
except Exception as exc:
    print(f">>> create_embeddings_client()/embed() RAISED: {exc!r}")
    traceback.print_exc()

print()
print("=" * 70)
print("DONE — read from the top: the first '>>> STOP' or '>>> FAILURE' line")
print("is your actual root cause.")
print("=" * 70)