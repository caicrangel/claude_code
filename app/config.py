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
    # NFeDistribuicaoDFe é nacional: retorna NFe emitidas contra o seu CNPJ
    # de qualquer UF. SEFAZ_UF aqui é apenas a UF do consultante (cUFAutor),
    # usada para roteamento interno da SEFAZ - NÃO filtra emitente.
    CERT_PATH: str = "/app/certs/certificado.pfx"
    CERT_PASSWORD: str = ""
    SEFAZ_AMBIENTE: int = 1
    SEFAZ_UF: str = "SP"

    SYNC_INTERVALO_HORAS: int = 1  # consultar SEFAZ a cada X horas (default: 1h)
    MIN_SEFAZ_INTERVAL: int = 60   # seg. mínimos entre consultas (anti-throttle 656)

    # Localização
    TZ: str = "America/Sao_Paulo"

    # Logo - URL completa OU caminho relativo a /static (ex: "empresa.png").
    # Vazio = mostra só o nome da empresa.
    # EMPRESA_LOGO é o fallback usado quando a versão específica do tema
    # (LIGHT/DARK) não está configurada. Para alternância automática:
    #   - EMPRESA_LOGO_LIGHT: logo usada no tema claro (geralmente versão escura)
    #   - EMPRESA_LOGO_DARK:  logo usada no tema escuro (geralmente versão clara)
    EMPRESA_LOGO: str = ""
    EMPRESA_LOGO_LIGHT: str = ""
    EMPRESA_LOGO_DARK: str = ""

    # SMTP
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "alertas@empresa.com.br"
    SMTP_TLS: bool = True

    # Telegram (alertas via bot)
    TELEGRAM_ENABLED: bool = False
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    ALERT_THRESHOLDS: str = "70,85,95,100"

    @property
    def thresholds(self) -> list[int]:
        return [int(x) for x in self.ALERT_THRESHOLDS.split(",") if x.strip()]

    @property
    def cnpj_limpo(self) -> str:
        return "".join(c for c in self.EMPRESA_CNPJ if c.isdigit())


settings = Settings()
