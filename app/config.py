from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql+psycopg2://fuel:fuel@db:5432/fuel"
    SECRET_KEY: str = "change-me"

    ADMIN_EMAIL: str = "admin@empresa.com.br"
    ADMIN_PASSWORD: str = "trocar123"

    CERT_PATH: str = "/app/certs/certificado.pfx"
    CERT_PASSWORD: str = ""
    SEFAZ_AMBIENTE: int = 1
    SEFAZ_UF: str = "SP"

    POLL_INTERVAL: int = 900

    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "alertas@empresa.com.br"
    SMTP_TLS: bool = True

    ALERT_THRESHOLDS: str = "70,85,95,100"

    @property
    def thresholds(self) -> list[int]:
        return [int(x) for x in self.ALERT_THRESHOLDS.split(",") if x.strip()]


settings = Settings()
