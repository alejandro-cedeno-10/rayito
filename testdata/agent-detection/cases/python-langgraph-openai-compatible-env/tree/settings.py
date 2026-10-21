from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_")

    model: str = ""
    base_url: str = ""
    api_key: str = ""


settings = Settings()
