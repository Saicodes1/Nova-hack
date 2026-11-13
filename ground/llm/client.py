import os

from openai import AsyncOpenAI


def offline() -> bool:
    """OFFLINE=1: demo mode without network (pinned scenes, cached explanations)."""
    return os.getenv("OFFLINE", "").strip().lower() in {"1", "true", "yes"}


def configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip()) and not offline()


def client():
    return AsyncOpenAI(timeout=20, max_retries=1)


def model(tier: str):
    return os.getenv(f"OPENAI_MODEL_{tier}", "").strip() or "gpt-4.1-mini"
