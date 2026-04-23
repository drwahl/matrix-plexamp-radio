from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    plex_url: str
    plex_token: str
    lastfm_api_key: str = ""
    matrix_homeserver: str
    matrix_token: str
    matrix_user_id: str
    matrix_room_id: str
    allowed_matrix_users: str = ""
    stream_url: str = ""
    liquidsoap_host: str = "liquidsoap"
    liquidsoap_port: int = 1234
    # litellm model string — prefix selects the provider:
    #   "anthropic/claude-haiku-4-5-20251001"  → Anthropic API
    #   "ollama/llama3.2"                       → local Ollama
    #   "openai/gpt-4o-mini"                    → OpenAI
    #   "openai/mistral"                        → any OpenAI-compatible endpoint (set ai_base_url)
    ai_model: str = ""
    ai_api_key: str = ""
    ai_base_url: str = ""
    session_secret: str = ""   # HMAC key for session cookies; auto-generated if blank

    @property
    def allowed_users_list(self) -> list[str]:
        return [u.strip() for u in self.allowed_matrix_users.split(",") if u.strip()]


settings = Settings()
