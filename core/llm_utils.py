"""
LLM utility functions - Ollama API calls shared across pipeline modules.
"""
import sys
import requests
from core import config


def call_ollama(prompt: str, temperature: float = None, system: str = None) -> str:
    """Call Ollama API for LLM inference."""
    url = f"{config.OLLAMA_BASE_URL}/api/generate"
    num_predict = getattr(config, "LLM_NUM_PREDICT", 8192)
    if temperature is None:
        temperature = float(getattr(config, "LLM_IDENTIFY_TEMPERATURE", 0.3))
    payload = {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "think": False,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict}
    }
    if system:
        payload["system"] = system
    try:
        resp = requests.post(url, json=payload, timeout=300)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except requests.exceptions.ConnectionError:
        print(f"[LLM] Cannot connect to Ollama ({config.OLLAMA_BASE_URL})")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("[LLM] Ollama request timeout")
        return ""
    except Exception as e:
        print(f"[LLM] Ollama error: {e}")
        return ""
