"""Configuration loaded from the environment.

Every tunable lives here so that no module has to reach into ``os.environ``
directly. Settings are read once at startup and passed down explicitly, which
keeps the provider and integration modules pure functions of their inputs and
therefore easy to test.

Locally the values come from ``.env.local``. In a container (Railway, Docker,
any PaaS) that file does not exist and the platform injects the same names as
real environment variables instead — ``load_dotenv`` simply finds nothing to
load, so exactly one code path serves both.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Resolve .env.local against the project root rather than the working directory,
# so the agent starts correctly no matter where it is launched from.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env.local"

# Credentials the voice pipeline cannot start without, mapped to where to get them.
# ASCII only: these are printed to the Windows console, which is cp1252.
REQUIRED_ENV = {
    "LIVEKIT_URL": "LiveKit Cloud project URL (wss://...)",
    "LIVEKIT_API_KEY": "LiveKit Cloud API key",
    "LIVEKIT_API_SECRET": "LiveKit Cloud API secret",
    "DEEPSEEK_API_KEY": "DeepSeek API key - https://platform.deepseek.com/api_keys",
    "CARTESIA_API_KEY": "Cartesia STT API key - https://play.cartesia.ai/keys",
    "ELEVENLABS_API_KEY": "ElevenLabs TTS API key - https://elevenlabs.io/app/settings/api-keys",
}


def load_env() -> None:
    """Populate os.environ from .env.local. Safe to call more than once."""
    load_dotenv(ENV_FILE)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_tuple(name: str) -> tuple[str, ...]:
    """Comma-separated list, whitespace tolerant. Empty entries are dropped."""
    return tuple(part.strip() for part in _env(name).split(",") if part.strip())


def _platform_port() -> int:
    """The port a PaaS told us to listen on, or 0 when we are not on one.

    Railway, Render, Fly and Heroku all assign the port at run time and inject
    it as ``PORT``; a service that ignores it never passes a health check.
    """
    return _env_int("PORT", 0)


@dataclass(frozen=True)
class LLMSettings:
    api_key: str = ""
    # deepseek-chat is the current non-reasoning V3 model: cheapest per token
    # and the lowest latency of the DeepSeek line, which matters for voice.
    model: str = "deepseek-chat"
    temperature: float = 0.6

    @staticmethod
    def from_env() -> LLMSettings:
        return LLMSettings(
            api_key=_env("DEEPSEEK_API_KEY"),
            model=_env("DEEPSEEK_MODEL") or "deepseek-chat",
            temperature=_env_float("LLM_TEMPERATURE", 0.6),
        )


@dataclass(frozen=True)
class STTSettings:
    api_key: str = ""
    # ink-2 (the plugin default for English) detects turns itself, which is what
    # lets the session use STT endpointing.
    model: str = ""

    @staticmethod
    def from_env() -> STTSettings:
        return STTSettings(
            api_key=_env("CARTESIA_API_KEY"), model=_env("CARTESIA_STT_MODEL")
        )


@dataclass(frozen=True)
class TTSSettings:
    """Voice output.

    Two vendors are supported because a voice agent with no voice is a dead
    agent: when one account runs out of credit, ``TTS_PROVIDER`` moves the
    whole pipeline to the other without touching code. Cartesia needs no new
    credential — it is the same key the STT already uses.
    """

    # "elevenlabs" (default) or "cartesia".
    provider: str = "elevenlabs"

    api_key: str = ""
    # flash is ElevenLabs' lowest-latency model; the plugin defaults to turbo,
    # which sounds marginally better but adds noticeable lag.
    model: str = "eleven_flash_v2_5"
    # Bella. Others: Adam (pNInz6obpgDQGcFmaJgB), Jessica (cgSgspJ2msm6clMCkdW9)
    voice_id: str = "EXAVITQu4vr4xnSDxMaL"

    # Cartesia, used when provider="cartesia".
    cartesia_api_key: str = ""
    cartesia_model: str = "sonic-2"
    # Nandi - Poised Concierge. `--list-voices` on the Cartesia API shows more.
    cartesia_voice: str = "33d406dd-ff6f-4be7-a7f5-8b1ba183b3e4"

    @property
    def uses_cartesia(self) -> bool:
        return self.provider.lower() == "cartesia"

    @staticmethod
    def from_env() -> TTSSettings:
        return TTSSettings(
            provider=(_env("TTS_PROVIDER") or "elevenlabs").lower(),
            api_key=_env("ELEVENLABS_API_KEY") or _env("ELEVEN_API_KEY"),
            model=_env("ELEVENLABS_MODEL") or "eleven_flash_v2_5",
            voice_id=_env("ELEVENLABS_VOICE_ID") or "EXAVITQu4vr4xnSDxMaL",
            cartesia_api_key=_env("CARTESIA_API_KEY"),
            cartesia_model=_env("CARTESIA_TTS_MODEL") or "sonic-2",
            cartesia_voice=_env("CARTESIA_VOICE_ID")
            or "33d406dd-ff6f-4be7-a7f5-8b1ba183b3e4",
        )


@dataclass(frozen=True)
class EmbeddingSettings:
    """Gemini embeddings.

    Uses Google's gemini-embedding-001 model.
    """

    api_key: str = ""
    model: str = "models/gemini-embedding-001"

    @staticmethod
    def from_env() -> EmbeddingSettings:
        return EmbeddingSettings(
            api_key=_env("GEMINI_API_KEY"),
            model=_env("EMBEDDING_MODEL") or "models/gemini-embedding-001",
        )


@dataclass(frozen=True)
class RagSettings:
    """Qdrant Cloud knowledge base.

    RAG is optional: with no QDRANT_URL the agent runs exactly as before, so the
    voice pipeline is never blocked on the knowledge base being set up.
    """

    url: str = ""
    api_key: str = ""
    collection: str = "ai_caller_knowledge"
    knowledge_dir: Path = PROJECT_ROOT / "knowledge"

    # How retrieved context reaches the LLM:
    #   "auto" injects it before generation (no extra LLM round-trip, lowest latency)
    #   "tool" exposes a search tool the LLM chooses to call (more precise, slower)
    #   "both" does both; "off" disables retrieval but keeps ingestion working.
    mode: str = "auto"

    top_k: int = 4
    # Cosine similarity floor. Below this, context is dropped rather than
    # injected, so unrelated small talk doesn't get polluted with documents.
    #
    # Calibrated for gemini-embedding-001, which has a high similarity floor:
    # measured against the shipped knowledge base, unrelated questions ("tell
    # me a joke about penguins", "what is the weather today") score 0.56-0.59
    # while a real match scores 0.72. The old 0.5 came from bge-small and sits
    # under that noise floor, which injected documents into every single turn.
    # Re-measure if EMBEDDING_MODEL changes; thresholds are model-specific.
    score_threshold: float = 0.65
    # Retrieval runs on the critical path of every turn; never let it stall a call.
    timeout_s: float = 3.0
    chunk_size: int = 800
    chunk_overlap: int = 120

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def injects_context(self) -> bool:
        return self.enabled and self.mode in ("auto", "both")

    @property
    def exposes_tool(self) -> bool:
        return self.enabled and self.mode in ("tool", "both")

    @staticmethod
    def from_env() -> RagSettings:
        knowledge = _env("KNOWLEDGE_DIR")
        return RagSettings(
            url=_env("QDRANT_URL"),
            api_key=_env("QDRANT_API_KEY"),
            collection=_env("QDRANT_COLLECTION") or "ai_caller_knowledge",
            knowledge_dir=Path(knowledge) if knowledge else PROJECT_ROOT / "knowledge",
            mode=(_env("RAG_MODE") or "auto").lower(),
            top_k=_env_int("RAG_TOP_K", 4),
            score_threshold=_env_float("RAG_SCORE_THRESHOLD", 0.5),
            timeout_s=_env_float("RAG_TIMEOUT_S", 3.0),
            chunk_size=_env_int("RAG_CHUNK_SIZE", 800),
            chunk_overlap=_env_int("RAG_CHUNK_OVERLAP", 120),
        )


@dataclass(frozen=True)
class LiveKitSettings:
    """LiveKit Cloud credentials.

    The SIP and Phone Number services need these explicitly; the agent worker
    itself reads them from the environment through the LiveKit CLI.
    """

    url: str = ""
    api_key: str = ""
    api_secret: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url and self.api_key and self.api_secret)

    @staticmethod
    def from_env() -> LiveKitSettings:
        return LiveKitSettings(
            url=_env("LIVEKIT_URL"),
            api_key=_env("LIVEKIT_API_KEY"),
            api_secret=_env("LIVEKIT_API_SECRET"),
        )


@dataclass(frozen=True)
class CallPolicySettings:
    """Guardrails applied to every call before it is placed.

    These are enforced in code rather than in the prompt: a dialer that can be
    talked into calling an emergency line is a safety problem, not a prompting
    problem. See ``telephony/policy.py``.
    """

    # Countries the dialer may call, as E.164 calling codes ("971", "91").
    # Empty means "no country restriction".
    allowed_countries: tuple[str, ...] = ()
    # When set, only these exact numbers may be dialled — the safest setting
    # while testing, since a wrong digit cannot reach a stranger.
    allowed_numbers: tuple[str, ...] = ()
    blocked_numbers: tuple[str, ...] = ()

    max_concurrent_calls: int = 5
    calls_per_minute: int = 10
    # Hard ceiling handed to LiveKit SIP, so a stuck call cannot bill forever.
    max_call_duration_s: int = 900
    ring_timeout_s: int = 30

    @staticmethod
    def from_env() -> CallPolicySettings:
        return CallPolicySettings(
            allowed_countries=tuple(
                code.lstrip("+") for code in _env_tuple("TELEPHONY_ALLOWED_COUNTRIES")
            ),
            allowed_numbers=_env_tuple("TELEPHONY_ALLOWED_NUMBERS"),
            blocked_numbers=_env_tuple("TELEPHONY_BLOCKED_NUMBERS"),
            max_concurrent_calls=_env_int("TELEPHONY_MAX_CONCURRENT_CALLS", 5),
            calls_per_minute=_env_int("TELEPHONY_CALLS_PER_MINUTE", 10),
            max_call_duration_s=_env_int("TELEPHONY_MAX_CALL_DURATION_S", 900),
            ring_timeout_s=_env_int("TELEPHONY_RING_TIMEOUT_S", 30),
        )


@dataclass(frozen=True)
class TelephonySettings:
    """Phone calls through LiveKit Phone Numbers and LiveKit SIP.

    Needs nothing beyond the LiveKit credentials the agent already uses:
    LiveKit rents the numbers and routes the calls, so there is no carrier
    account and no SIP trunk to wire up for inbound.

    Outbound is a separate capability. LiveKit Phone Numbers is inbound-only
    today, so placing calls requires a LiveKit *outbound trunk* pointed at a
    carrier (``LIVEKIT_SIP_OUTBOUND_TRUNK_ID``). Without one, the dialer
    reports outbound as unavailable and everything else still works.
    """

    livekit: LiveKitSettings = field(default_factory=LiveKitSettings)
    policy: CallPolicySettings = field(default_factory=CallPolicySettings)

    # LiveKit SIP objects. The dispatch rule is created by
    # scripts/setup_telephony.py; the outbound trunk, if any, is yours.
    dispatch_rule_id: str = ""
    outbound_trunk_id: str = ""

    # Caller ID used for outbound when a request does not name one.
    caller_id: str = ""
    # Rooms for phone calls are named "<prefix>-<something>", which is also how
    # the control plane finds live calls to list.
    room_prefix: str = "call"

    # Control-plane HTTP server (python -m ai_caller.telephony.api).
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    # Shared secret required in the X-API-Key header. Empty disables the check,
    # which is only safe on a loopback bind.
    api_key: str = ""
    # Browsers calling the control plane directly need this; the bundled Vite
    # dev server proxies instead, and in production the API and the UI share an
    # origin, so it defaults to closed.
    cors_origins: tuple[str, ...] = ()
    # Acknowledges that a non-loopback bind with no API key is intended. A PaaS
    # puts this endpoint on the public internet by design, so the usual refusal
    # would be wrong there — but it has to be asked for, never assumed.
    allow_public: bool = False

    @property
    def enabled(self) -> bool:
        """True when the control plane can talk to LiveKit at all."""
        return self.livekit.configured

    @property
    def can_dial_out(self) -> bool:
        """True when outbound calls can actually be placed.

        LiveKit Phone Numbers does not support outbound yet, so this is false
        until an outbound trunk exists. See
        https://docs.livekit.io/telephony/making-calls/outbound-trunk
        """
        return self.enabled and bool(self.outbound_trunk_id)

    @staticmethod
    def from_env() -> TelephonySettings:
        # On a PaaS the port is assigned at run time and the container only
        # receives traffic on 0.0.0.0, so $PORT also tells us which default
        # bind address is correct. Explicit TELEPHONY_* values still win.
        platform_port = _platform_port()
        return TelephonySettings(
            livekit=LiveKitSettings.from_env(),
            policy=CallPolicySettings.from_env(),
            dispatch_rule_id=_env("LIVEKIT_SIP_DISPATCH_RULE_ID"),
            outbound_trunk_id=_env("LIVEKIT_SIP_OUTBOUND_TRUNK_ID"),
            caller_id=_env("TELEPHONY_CALLER_ID"),
            room_prefix=_env("TELEPHONY_ROOM_PREFIX") or "call",
            api_host=_env("TELEPHONY_API_HOST")
            or ("0.0.0.0" if platform_port else "127.0.0.1"),
            api_port=platform_port or _env_int("TELEPHONY_API_PORT", 8080),
            api_key=_env("TELEPHONY_API_KEY"),
            cors_origins=_env_tuple("TELEPHONY_CORS_ORIGINS"),
            allow_public=_env_bool("TELEPHONY_ALLOW_PUBLIC", False),
        )


@dataclass(frozen=True)
class WorkerSettings:
    """How much the worker keeps warm, and how hard it may push the box.

    Each idle job process is a full copy of the agent — every import plus the
    prewarmed integrations, measured at ~220 MB. LiveKit sizes this pool from
    the CPU count, which is right for a dedicated machine and fatal in a
    container: a 16-core host asks for 16 processes, ~3.5 GB, and the kernel
    SIGKILLs them. All the caller hears is silence, and the only evidence is
    ``process exited with non-zero exit code -9``.

    So the pool is set explicitly here rather than inferred. One warm process
    answers the next call without paying the ~2s import cost, which is the
    cost that matters when someone is already on the line.
    """

    # Job processes kept ready. 0 trades first-call latency for ~220 MB.
    idle_processes: int = 1
    # Per-job ceiling. Above it LiveKit ends the job itself and says why,
    # instead of leaving the kernel to kill it with no explanation. 0 is off.
    job_memory_limit_mb: int = 0
    job_memory_warn_mb: int = 500

    @staticmethod
    def from_env() -> WorkerSettings:
        return WorkerSettings(
            idle_processes=_env_int("AGENT_IDLE_PROCESSES", 1),
            job_memory_limit_mb=_env_int("AGENT_JOB_MEMORY_LIMIT_MB", 0),
            job_memory_warn_mb=_env_int("AGENT_JOB_MEMORY_WARN_MB", 500),
        )


@dataclass(frozen=True)
class WebSettings:
    """The browser-facing half of the app.

    In development the Vite dev server mints connection details and proxies
    ``/api/telephony`` (see ``frontend/vite.config.js``). There is no dev server
    in production, so the same two jobs are done by ``ai_caller.webapp``: it
    serves the built SPA and mints the same tokens, which keeps the browser on
    one origin and the LiveKit API secret on the server in both environments.
    """

    # Output of `npm run build`. Absent in development, where Vite serves the
    # app from source instead — the API then runs without a UI attached.
    static_dir: Path = PROJECT_ROOT / "frontend" / "dist"
    # How long a browser's room token stays valid. Short: it is minted on
    # demand, so there is no reason for one to outlive the page that asked.
    token_ttl_minutes: int = 15

    @property
    def has_ui(self) -> bool:
        return self.static_dir.is_dir() and (self.static_dir / "index.html").is_file()

    @staticmethod
    def from_env() -> WebSettings:
        static = _env("FRONTEND_DIST")
        return WebSettings(
            static_dir=Path(static) if static else PROJECT_ROOT / "frontend" / "dist",
            token_ttl_minutes=_env_int("WEB_TOKEN_TTL_MINUTES", 15),
        )


@dataclass(frozen=True)
class Settings:
    """Everything the app needs, resolved once at startup."""

    agent_name: str = "my-agent"
    llm: LLMSettings = field(default_factory=LLMSettings)
    stt: STTSettings = field(default_factory=STTSettings)
    tts: TTSSettings = field(default_factory=TTSSettings)
    embeddings: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    rag: RagSettings = field(default_factory=RagSettings)
    telephony: TelephonySettings = field(default_factory=TelephonySettings)
    web: WebSettings = field(default_factory=WebSettings)
    worker: WorkerSettings = field(default_factory=WorkerSettings)

    @staticmethod
    def from_env() -> Settings:
        load_env()
        return Settings(
            agent_name=_env("AGENT_NAME") or "my-agent",
            llm=LLMSettings.from_env(),
            stt=STTSettings.from_env(),
            tts=TTSSettings.from_env(),
            embeddings=EmbeddingSettings.from_env(),
            rag=RagSettings.from_env(),
            telephony=TelephonySettings.from_env(),
            web=WebSettings.from_env(),
            worker=WorkerSettings.from_env(),
        )


def check_env(required: dict[str, str] | None = None) -> None:
    """Fail fast with a readable message instead of crashing mid-call."""
    load_env()
    checks = REQUIRED_ENV if required is None else required
    missing = [
        f"  - {name}: {hint}" for name, hint in checks.items() if not os.getenv(name)
    ]
    if not missing:
        return

    # Point at whichever source is actually in play: a container has no
    # .env.local, and telling someone to edit a file that does not exist is
    # worse than saying nothing.
    if ENV_FILE.is_file():
        where = ["ERROR: missing credentials in .env.local", f"       ({ENV_FILE})"]
        remedy = "Fill these in, then start the agent again."
    else:
        where = ["ERROR: missing credentials in the environment"]
        remedy = "Set these as environment variables, then start the agent again."

    print(
        "\n".join(["", *where, "", *missing, "", remedy, ""]),
        file=sys.stderr,
    )
    raise SystemExit(1)
