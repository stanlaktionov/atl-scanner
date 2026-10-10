"""Shared httpx client: timeouts, retries with exponential backoff, Retry-After
handling for 429/418/5xx, integration with rate limiters and the raw disk cache."""
