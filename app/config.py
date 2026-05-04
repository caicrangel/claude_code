from datetime import date

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql+psycopg2://fuel:fuel@db:5432/fuel"
    SECRET_KEY: str = "change-me"

    # Login do painel
    AUTH_EMAIL: str = "admin@empresa.com.br"
    AUTH_PASSWORD: str = "trocar123"

    # Dados da empresa (1 deploy = 1 empresa)
    EMPRESA_NOME: str = "Minha Empresa"
    EMPRESA_CNPJ: str = ""               # apenas dígitos
    COTA_LITROS: float = 1_220_000.0
    PERIODO_INICIO: date = date(2025, 1, 1)
    PERIODO_FIM: date = date(2025, 6, 30)
    EMAIL_ALERTAS: str = ""

    # SEFAZ
    CERT_PATH: str = "/app/certs/certificado.pfx"
    CERT_PASSWORD: str = ""
    SEFAZ_AMBIENTE: int = 1
    SEFAZ_UF: str = "SP"

    # SIEG (https://api.sieg.com) — fonte alternativa via cofre da contabilidade.
    # Quando preenchido, é usado como fonte primária no lugar do SEFAZ direto.
    SIEG_API_KEY: str = ""

    POLL_INTERVAL: int = 900
    MIN_SEFAZ_INTERVAL: int = 60   # seg. mínimos entre consultas (anti-throttle 656)

    # SMTP
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

    @property
    def cnpj_limpo(self) -> str:
        return "".join(c for c in self.EMPRESA_CNPJ if c.isdigit())


settings = Settings()
