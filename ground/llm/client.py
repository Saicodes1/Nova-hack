import os

from openai import AsyncOpenAI


def configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def client():
    return AsyncOpenAI(timeout=20, max_retries=1)


def model(tier: str):
    return os.getenv(f"OPENAI_MODEL_{tier}", "").strip() or "gpt-4.1-mini"
