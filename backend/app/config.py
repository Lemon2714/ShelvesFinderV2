from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- API Keys ---
    openai_api_key: str | None = None
    serper_api_key: str | None = None
    webscraping_api_key: str | None = None

    # Anthropic API key (required when llm_provider = "claude")
    anthropic_api_key: str | None = None

    # --- LLM Provider ---
    # "openai"  → use OpenAI for all chat/tool-calling tasks
    # "claude"  → use Anthropic Claude for chat/tool-calling tasks
    # NOTE: embeddings always use OpenAI regardless of this setting,
    #       because Claude does not provide an embeddings API.
    llm_provider: str = "openai"

    # --- Caching ---
    redis_url: str | None = None
    enable_cache: bool = False

    # --- OpenAI Models ---
    openai_chat_model: str = "gpt-3.5-turbo"           # used by v1 keyword agent
    openai_embedding_model: str = "text-embedding-3-small"

    # v2 orchestrator model (gpt-4o-mini = cheap + capable; swap to gpt-4o for max quality)
    openai_orchestrator_model: str = "gpt-4o-mini"

    # --- Claude Models ---
    # Used when llm_provider = "claude"
    # Claude 4.5 models (available on this account):
    #   claude-haiku-4-5    — fastest + cheapest, good for keyword generation
    #   claude-sonnet-4-5   — balanced reasoning, ideal for orchestration
    #   claude-opus-4-5     — most capable (higher cost)
    claude_chat_model: str = "claude-haiku-4-5"        # keyword agent (fast + cheap)
    claude_orchestrator_model: str = "claude-sonnet-4-5" # ReAct loop (better reasoning)

    # --- Google Integration ---
    google_drive_folder_id: str | None = None
    google_application_credentials: str | None = None
    google_sheet_id: str | None = None
    use_google_sheets: bool = False

    # --- Email Settings (SMTP) ---
    smtp_server: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_cc_email: str | None = None

    # --- v2 Agent Loop Settings ---
    # Maximum number of ReAct iterations per analysis run
    v2_max_rounds: int = 5
    # Stop when this many missing pages have been found
    v2_target_missing_count: int = 3
    # Hard ceiling on LLM spend per request (USD)
    v2_budget_limit: float = 0.50

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------------------
    # Convenience helpers
    # ---------------------------------------------------------------------------

    @property
    def using_claude(self) -> bool:
        """True when Claude is the active LLM provider."""
        return self.llm_provider.lower() == "claude"

    @property
    def active_chat_model(self) -> str:
        """Returns the chat model name for the active provider."""
        return self.claude_chat_model if self.using_claude else self.openai_chat_model

    @property
    def active_orchestrator_model(self) -> str:
        """Returns the orchestrator model name for the active provider."""
        return self.claude_orchestrator_model if self.using_claude else self.openai_orchestrator_model


settings = Settings()
