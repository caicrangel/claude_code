# Cota Diesel — Automação NFe (single-tenant)

Automação que monitora as notas fiscais de **óleo diesel** emitidas contra
o CNPJ da empresa, soma a galonagem (litros) consumida no período da cota
de ICMS reduzido e dispara alertas por **email** + exibe **dashboard web
com login** quando o consumo se aproxima do teto.

**1 deploy = 1 empresa.** Pra adicionar outra empresa: copie a pasta, ajuste
o `.env`, suba outro `docker compose` em outra porta.

## Como funciona

1. **Worker** consulta o webservice oficial **NFeDistribuicaoDFe** da SEFAZ a
   cada `POLL_INTERVAL` segundos, usando certificado digital A1 (.pfx).
2. As NFe novas são parseadas, filtrando itens de diesel pelo NCM
   (`27101921/22/31`) e palavras-chave em `xProd`.
3. Os litros (`qCom`) são somados dentro do período. Se o consumo cruza
   70/85/95/100% (configurável), um email é disparado e fica registrado
   pra não duplicar.
4. **Dashboard** com login mostra barra de progresso, KPIs e últimas notas.
   Auto-refresh a cada 60s + botão de sincronização manual.

## Pré-requisitos

- **Certificado digital A1 (.pfx)** da empresa.
- **Docker** + **Docker Compose**.

## Setup

```bash
cp .env.example .env
# edite .env: SECRET_KEY, AUTH_EMAIL/AUTH_PASSWORD, EMPRESA_*, SMTP_*

mkdir -p certs
cp /caminho/do/certificado.pfx certs/certificado.pfx

docker compose up -d --build
```

Acesse `http://localhost:8000`, faça login com `AUTH_EMAIL`/`AUTH_PASSWORD`.

## Adicionar uma nova empresa

```bash
cp -r cota-diesel/ cota-diesel-empresa-b/
cd cota-diesel-empresa-b/
# edite .env: troque EMPRESA_*, AUTH_*, SECRET_KEY, certificado, e
# mude a porta no docker-compose.yml (ex.: 8001:8000)
mkdir certs && cp /caminho/empresa-b.pfx certs/certificado.pfx
docker compose up -d --build
```

Cada deploy tem seu Postgres isolado, seu certificado, suas credenciais.

## Variáveis principais (`.env`)

| Var | Descrição |
| --- | --- |
| `EMPRESA_NOME` / `EMPRESA_CNPJ` | Identificação da empresa. CNPJ sem pontuação. |
| `COTA_LITROS` | Litros totais da cota (ex.: `1220000`). |
| `PERIODO_INICIO` / `PERIODO_FIM` | Datas do período em ISO (`YYYY-MM-DD`). |
| `EMAIL_ALERTAS` | Destinatário dos alertas por email. |
| `AUTH_EMAIL` / `AUTH_PASSWORD` | Login do painel. |
| `CERT_PATH` / `CERT_PASSWORD` | Certificado A1 (montado em `./certs`). |
| `SEFAZ_AMBIENTE` | `1` produção, `2` homologação. |
| `SEFAZ_UF` | UF do consultante (ex.: `SP`). |
| `POLL_INTERVAL` | Intervalo do polling em segundos (default 900). |
| `ALERT_THRESHOLDS` | `70,85,95,100` — % que disparam email. |
| `SMTP_*` | Configuração SMTP. |

## Estrutura

```
app/
  main.py              # FastAPI: login + dashboard + /sync
  worker.py            # APScheduler: polling periódico
  config.py            # Settings (lê .env)
  database.py
  models.py            # NotaFiscal / Alerta / State (chave-valor p/ ultNSU)
  auth.py              # Login simples comparando com .env
  services/
    sefaz.py           # Cliente NFeDistribuicaoDFe (SOAP + mTLS A1)
    parser.py          # Parse XML NFe + filtro diesel
    quota.py           # Soma litros, % da cota, dispara alerta
    email.py           # SMTP
    ingest.py          # Orquestração SEFAZ → DB → cota
  templates/           # login.html, dashboard.html, base.html
  static/app.css
docker-compose.yml     # app + worker + postgres
Dockerfile
```

## Pontos de extensão

- **Resumo (`resNFe`)**: o MVP só processa NFe completa. Se a SEFAZ devolver
  apenas resumo, evolua pra acionar manifestação de ciência (evento 210210)
  e/ou consulta `consNFe` por chave em `services/sefaz.py`.
- **Eventos de cancelamento**: hoje uma NFe cancelada continua somando.
  Escute `procEventoNFe` e marque a nota como cancelada.
- **Filtro de diesel**: ajuste `NCM_DIESEL_PREFIXES` e `DIESEL_KEYWORDS` em
  `services/parser.py` se sua operação tiver outras nuances.

## Segurança

- Sessão via cookie assinado (`itsdangerous`).
- Login comparado com `secrets.compare_digest` contra `.env`.
- Certificado `.pfx` montado read-only via volume; **nunca** vai pra imagem.
- `.env` e `certs/` no `.gitignore`.
