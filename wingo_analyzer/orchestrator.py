"""Provider orchestration: Gemini is the main free provider, OpenRouter's free
NVIDIA Nemotron 3 Ultra is the second opinion, and Groq Cloud is the last
fallback. The three are walked in a continuous loop.

Design
------
* every call starts at the head of the chain (Gemini) and walks it round-robin;
* a provider that errors goes into an exponential cooldown (5s -> 60s) and the
  call immediately falls through to the next one in the loop - so a Gemini
  failure, a rate-limit or an outage simply means "answered by Nemotron";
* a cooldown expires by itself, the next call tries that provider again, and a
  successful answer resets it (self-healing, no restart needed);
* hard auth failures disable a provider for 10 minutes instead of hammering it;
* ``health()`` feeds /status, the admin panel and the public status page.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import requests

from .config import Config
from .errors import AIError

log = logging.getLogger("wingo.ai")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class ProviderError(Exception):
    def __init__(self, message: str, *, kind: str = "transient"):
        super().__init__(message)
        self.kind = kind  # transient | ratelimit | auth | badmodel | empty


@dataclass
class AIResult:
    text: str
    provider: str
    model: str
    latency_ms: int
    fallback: bool = False
    note: str = ""


@dataclass
class ProviderStats:
    calls: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    last_error: str = ""
    last_ok: float = 0.0
    cooldown_until: float = 0.0
    cursor: int = 0
    disabled_until: float = 0.0
    current_model: str = ""
    history: list = field(default_factory=list)


class Provider:
    name = "provider"
    kind = "chat"

    def __init__(self, key: str, models: list[str], timeout: int = 22, temperature: float = 0.85):
        self.key = key
        self.models = [m for m in models if m]
        self.timeout = timeout
        self.temperature = temperature
        self.stats = ProviderStats()
        self.stats.current_model = self.models[0] if self.models else ""

    # ---------------- health -------------------------------------------- #
    def cooldown_left(self) -> float:
        return max(0.0, self.stats.cooldown_until - time.time())

    def disabled_left(self) -> float:
        return max(0.0, self.stats.disabled_until - time.time())

    def ready(self) -> bool:
        return bool(self.key) and self.cooldown_left() <= 0 and self.disabled_left() <= 0

    def note_success(self, model: str) -> None:
        self.stats.calls += 1
        self.stats.consecutive_errors = 0
        self.stats.cooldown_until = 0.0
        self.stats.last_ok = time.time()
        self.stats.current_model = model
        self.stats.history.append(("ok", int(time.time())))
        self.stats.history = self.stats.history[-8:]

    def note_failure(self, error: ProviderError) -> None:
        self.stats.errors += 1
        self.stats.consecutive_errors += 1
        self.stats.last_error = str(error)[:180]
        self.stats.history.append(("err", int(time.time())))
        self.stats.history = self.stats.history[-8:]
        now = time.time()
        if error.kind == "auth":
            self.stats.disabled_until = now + 600
            log.error("%s auth/key rejected - disabled for 10 min: %s", self.name, error)
        elif error.kind == "ratelimit":
            self.stats.cooldown_until = now + min(90, 20 * self.stats.consecutive_errors)
            log.warning("%s rate limited, cooling down: %s", self.name, error)
        elif error.kind == "badmodel":
            self.stats.cursor += 1  # rotate past the broken model id
            self.stats.cooldown_until = now + 5
        else:
            backoff = min(60, 5 * (2 ** (self.stats.consecutive_errors - 1)))
            self.stats.cooldown_until = now + backoff
            log.warning("%s failed (%ss backoff): %s", self.name, backoff, error)

    # ---------------- calls --------------------------------------------- #
    def _models_in_order(self) -> list[str]:
        """Rotate the model list and cap how many we try in one call, so a
        caller never waits on more than two slow/free endpoints per visit."""
        if not self.models:
            return []
        start = self.stats.cursor % len(self.models)
        ordered = self.models[start:] + self.models[:start]
        return ordered[:2]

    def complete(self, system: str, user: str, *, max_tokens: int, temperature: float | None) -> tuple[str, str]:
        raise NotImplementedError

    def health(self) -> dict:
        return {
            "name": self.name,
            "configured": bool(self.key),
            "models": self.models,
            "model": self.stats.current_model or (self.models[0] if self.models else ""),
            "calls": self.stats.calls,
            "errors": self.stats.errors,
            "cooldown_s": int(self.cooldown_left()),
            "disabled_s": int(self.disabled_left()),
            "last_error": self.stats.last_error,
            "last_ok_ago": int(time.time() - self.stats.last_ok) if self.stats.last_ok else None,
        }


class GeminiProvider(Provider):
    name = "gemini"

    def complete(self, system: str, user: str, *, max_tokens: int, temperature: float | None):
        last_error: ProviderError | None = None
        for model in self._models_in_order():
            body = {
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    # thinking tokens share this budget on Gemini flash models,
                    # so leave the requested answer plenty of headroom
                    "maxOutputTokens": max(600, max_tokens * 6),
                    "temperature": self.temperature if temperature is None else temperature,
                },
            }
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            try:
                response = requests.post(
                    f"{GEMINI_BASE}/{model}:generateContent",
                    json=body,
                    headers={"Content-Type": "application/json", "x-goog-api-key": self.key},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                # one slow/broken model must not abort the rotation
                last_error = ProviderError(f"network: {exc}", kind="transient")
                self.stats.cursor += 1
                continue

            if response.status_code in (429, 503):
                # this model is rate-limited or overheated - rotate to the next
                # model in the same provider before cooling the provider down
                last_error = ProviderError(
                    f"HTTP {response.status_code} on {model}",
                    kind="ratelimit" if response.status_code == 429 else "transient",
                )
                self.stats.cursor += 1
                continue
            if response.status_code in (401, 403):
                last_error = ProviderError(f"HTTP {response.status_code} key rejected", kind="auth")
                break
            if response.status_code == 404:
                last_error = ProviderError(f"model {model} not found", kind="badmodel")
                self.stats.cursor += 1
                continue
            if response.status_code >= 500:
                last_error = ProviderError(f"HTTP {response.status_code}", kind="transient")
                break
            if response.status_code >= 400:
                body = response.text[:200]
                kind = "transient" if response.status_code >= 500 else "badmodel"
                if response.status_code in (400, 401, 403) and "api key" in body.lower():
                    kind = "auth"
                last_error = ProviderError(
                    f"HTTP {response.status_code}: {body}", kind=kind,
                )
                if kind in ("auth", "transient"):
                    break
                self.stats.cursor += 1
                continue

            try:
                data = response.json()
            except ValueError:
                last_error = ProviderError("non-JSON response", kind="transient")
                break

            if data.get("promptFeedback", {}).get("blockReason"):
                return ("The model declined this prompt: "
                        + str(data["promptFeedback"]["blockReason"]), model)

            candidates = data.get("candidates") or []
            parts = (candidates[0].get("content") or {}).get("parts") or [] if candidates else []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
            if not text:
                last_error = ProviderError("empty candidate", kind="empty")
                continue
            return text, model

        raise last_error or ProviderError("no model available", kind="badmodel")


class OpenRouterProvider(Provider):
    """OpenRouter free tier - NVIDIA Nemotron 3 Ultra leads the list.

    OpenAI-compatible endpoint, but free models live behind a strict daily quota
    and the router answers 429 quickly, so failures here rotate the model inside
    the provider first and only then cool the whole provider down.
    """

    name = "openrouter"

    def complete(self, system: str, user: str, *, max_tokens: int, temperature: float | None):
        last_error: ProviderError | None = None
        for model in self._models_in_order():
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": user})
            body = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": self.temperature if temperature is None else temperature,
                # free models are happier when we say we do not train on the chat
                "usage": {"include": False},
            }
            try:
                response = requests.post(
                    OPENROUTER_URL,
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + self.key,
                        # OpenRouter asks for both so free-quota usage is attributed
                        "HTTP-Referer": "https://dmfirst1.com",
                        "X-Title": "WINGO NEXUS AI",
                    },
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = ProviderError(f"network: {exc}", kind="transient")
                self.stats.cursor += 1
                continue

            if response.status_code in (402, 429, 503):
                last_error = ProviderError(
                    f"HTTP {response.status_code} on {model} (free quota)",
                    kind="ratelimit" if response.status_code == 429 else "transient",
                )
                self.stats.cursor += 1
                continue
            if response.status_code in (401, 403):
                last_error = ProviderError(f"HTTP {response.status_code} key rejected", kind="auth")
                break
            if response.status_code == 404:
                last_error = ProviderError(f"model {model} not found", kind="badmodel")
                self.stats.cursor += 1
                continue
            if response.status_code >= 500:
                last_error = ProviderError(f"HTTP {response.status_code}", kind="transient")
                break
            if response.status_code >= 400:
                last_error = ProviderError(
                    f"HTTP {response.status_code}: {response.text[:160]}",
                    kind="badmodel",
                )
                self.stats.cursor += 1
                continue

            try:
                data = response.json()
            except ValueError:
                last_error = ProviderError("non-JSON response", kind="transient")
                break

            choices = data.get("choices") or []
            text = ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
            if not text:
                # reasoning models sometimes park the visible text in ``reasoning``
                reasoning = ((choices[0].get("message") or {}).get("reasoning") or "").strip() \
                    if choices else ""
                if reasoning:
                    return reasoning, model
                last_error = ProviderError("empty choice", kind="empty")
                continue
            return text, model

        raise last_error or ProviderError("no model available", kind="badmodel")


class GroqProvider(Provider):
    name = "groq"

    def complete(self, system: str, user: str, *, max_tokens: int, temperature: float | None):
        last_error: ProviderError | None = None
        for model in self._models_in_order():
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": user})
            body = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": self.temperature if temperature is None else temperature,
            }
            try:
                response = requests.post(
                    GROQ_URL,
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + self.key,
                    },
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = ProviderError(f"network: {exc}", kind="transient")
                self.stats.cursor += 1
                continue

            if response.status_code in (429, 503):
                # rate-limited or overheated: rotate to the next model before
                # cooling the whole provider down
                last_error = ProviderError(
                    f"HTTP {response.status_code} on {model}",
                    kind="ratelimit" if response.status_code == 429 else "transient",
                )
                self.stats.cursor += 1
                continue
            if response.status_code in (401, 403):
                last_error = ProviderError(f"HTTP {response.status_code} key rejected", kind="auth")
                break
            if response.status_code == 404:
                last_error = ProviderError(f"model {model} not found", kind="badmodel")
                self.stats.cursor += 1
                continue
            if response.status_code >= 400:
                last_error = ProviderError(
                    f"HTTP {response.status_code}: {response.text[:160]}",
                    kind="transient" if response.status_code >= 500 else "badmodel",
                )
                if response.status_code >= 500:
                    break
                continue

            try:
                data = response.json()
            except ValueError:
                last_error = ProviderError("non-JSON response", kind="transient")
                break

            choices = data.get("choices") or []
            text = ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
            if not text:
                last_error = ProviderError("empty choice", kind="empty")
                continue
            return text, model

        raise last_error or ProviderError("no model available", kind="badmodel")


class Orchestrator:
    """Walks [Gemini, OpenRouter/Nemotron, Groq] in a loop - first usable answer wins."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.providers: list[Provider] = []
        if cfg.gemini_key:
            self.providers.append(
                GeminiProvider(cfg.gemini_key, cfg.gemini_models, cfg.ai_timeout, cfg.ai_temperature)
            )
        if cfg.openrouter_key:
            self.providers.append(
                OpenRouterProvider(cfg.openrouter_key, cfg.openrouter_models,
                                   cfg.ai_timeout, cfg.ai_temperature)
            )
        if cfg.groq_key:
            self.providers.append(
                GroqProvider(cfg.groq_key, cfg.groq_models, cfg.ai_timeout, cfg.ai_temperature)
            )
        self.calls = 0
        self.fallbacks = 0
        self.started = time.time()

    # ------------------------------------------------------------------ #
    @property
    def configured(self) -> list[str]:
        return [p.name for p in self.providers]

    @property
    def primary(self) -> str:
        return self.providers[0].name if self.providers else "-"

    def is_ready(self) -> bool:
        return any(p.ready() for p in self.providers)

    def complete(self, system: str, user: str, *, max_tokens: int = 300,
                 temperature: float | None = None) -> AIResult:
        if not self.providers:
            raise AIError("no AI provider configured "
                          "(set GEMINI_API_KEY / OPENROUTER_API_KEY / GROQ_API_KEY)")

        errors: list[str] = []
        now = time.time()
        # walk the chain twice so a provider cooling down is skipped, then
        # re-tried later in the very same loop once its cooldown lapses.
        for step in range(max(4, len(self.providers) * 2)):
            provider = self.providers[step % len(self.providers)]
            if not provider.ready():
                left = int(provider.cooldown_left() or provider.disabled_left())
                if left:
                    errors.append(f"{provider.name}: cooling down {left}s")
                continue
            started = time.time()
            try:
                text, model = provider.complete(
                    system, user, max_tokens=max_tokens, temperature=temperature
                )
            except ProviderError as exc:
                provider.note_failure(exc)
                errors.append(f"{provider.name}: {exc}")
                continue
            except Exception as exc:  # never let a provider kill a caller
                provider.note_failure(ProviderError(f"unexpected: {exc}"))
                errors.append(f"{provider.name}: {exc}")
                continue

            provider.note_success(model)
            self.calls += 1
            served_by = provider.name
            if step > 0 or any(not p.ready() for p in self.providers[:1]):
                self.fallbacks += 1
                log.info("ai failover: %s served by %s/%s", system[:18], served_by, model)
            return AIResult(
                text=text,
                provider=served_by,
                model=model,
                latency_ms=int((time.time() - started) * 1000),
                fallback=step > 0,
            )

        raise AIError(" | ".join(errors) or "all providers cooling down")

    def complete_or(self, system: str, user: str, fallback: str, **kwargs) -> AIResult:
        try:
            return self.complete(system, user, **kwargs)
        except AIError as exc:
            self.fallbacks += 1
            log.warning("ai chain exhausted, using local fallback: %s", exc)
            return AIResult(text=fallback, provider="local", model="heuristic", latency_ms=0,
                            fallback=True, note=str(exc)[:160])

    # ------------------------------------------------------------------ #
    def health(self) -> dict:
        known = {p.name: p for p in self.providers}
        display = []
        for name, factory in (
            ("gemini", lambda: GeminiProvider(self.cfg.gemini_key, self.cfg.gemini_models)),
            ("openrouter", lambda: OpenRouterProvider(self.cfg.openrouter_key,
                                                       self.cfg.openrouter_models)),
            ("groq", lambda: GroqProvider(self.cfg.groq_key, self.cfg.groq_models)),
        ):
            display.append(known[name] if name in known else factory())
        return {
            "ready": self.is_ready(),
            "primary": self.primary,
            "configured": self.configured,
            "missing": [name for name in ("gemini", "openrouter", "groq")
                        if name not in self.configured],
            "calls": self.calls,
            "fallbacks": self.fallbacks,
            "uptime_s": int(time.time() - self.started),
            "providers": [p.health() for p in display],
        }

    def probe_recovery(self) -> None:
        """Tiny ping against a provider whose cooldown just lapsed while unhealthy."""
        for provider in self.providers:
            if provider.stats.consecutive_errors == 0:
                continue
            if provider.cooldown_left() > 0 or provider.disabled_left() > 0:
                continue
            try:
                text, model = provider.complete("Reply with the single word OK.", "ping",
                                                max_tokens=8, temperature=0.0)
            except ProviderError as exc:
                provider.note_failure(exc)
                continue
            except Exception as exc:
                provider.note_failure(ProviderError(str(exc)))
                continue
            provider.note_success(model)
            log.info("%s recovered (model %s): %s", provider.name, model, text[:40])
