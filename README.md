# Cota Diesel — Automação NFe via SEFAZ (single-tenant)

Automação que monitora as notas fiscais de **óleo diesel** emitidas contra
o CNPJ da empresa, soma a galonagem (litros) consumida no período da cota
de ICMS reduzido e dispara alertas por **email** + exibe **dashboard web
com login** quando o consumo se aproxima do teto.

**1 deploy = 1 empresa.** Pra adicionar outra empresa: copie a pasta,
ajuste o `.env`, suba outro `docker compose` em outra porta.

## Arquitetura

Toda a integração é feita diretamente com a SEFAZ via webservices oficiais,
usando certificado digital A1 (mTLS):

| Webservice | Uso |
| --- | --- |
| `NFeDistribuicaoDFe` | polling sequencial via NSU (todas as NFe e eventos contra o CNPJ) |
| `NFeDistribuicaoDFe / consChNFe` | consulta a NFe completa após manifestação |
| `NFeRecepcaoEvento4` | envio do evento 210210 (ciência da operação) |

## Como funciona

1. **Worker** consulta `NFeDistribuicaoDFe` a cada `POLL_INTERVAL` segundos,
   iterando o NSU sequencial (estado guardado na tabela `state`).
2. Cada `docZip` retornado é classificado pelo `schema`:
   - **NFe completa (`procNFe`)** → parseada, itens de diesel filtrados por
     NCM `271019*` ou keywords (`DIESEL`, `S10`, `S500`, `B-S10`),
     gravados com `litros_diesel`.
   - **Resumo (`resNFe`)** → gravado com flag `is_resumo=True` (sem litros);
     fica visível no painel como “pendente” até a NFe completa chegar.
   - **Evento (`procEventoNFe`)** → aplicado:
     - `110111` cancelamento → marca `cancelada=True` (descontada da cota)
     - `210210` ciência → marca `manifestada=True`
3. Para destravar a NFe completa de um resumo é preciso **manifestar
   ciência** (evento 210210 assinado e enviado ao `NFeRecepcaoEvento4`).
   O app oferece dois caminhos:
   - **Manual**: botão “Manifestar” em cada linha pendente do dashboard.
   - **Automático**: setando `MANIFESTAR_AUTO=true` no `.env`, o worker
     manifesta todos os pendentes (até 50 por ciclo) e em seguida usa
     `consChNFe` pra puxar a NFe completa.
4. **Cota** (`services/quota.py`) soma `litros_diesel` apenas das notas
   completas e não canceladas, dentro do período. Em 70/85/95/100% da
   cota um email é disparado (deduplicação por threshold + período).
5. **Dashboard** (`/dashboard`) e **Relatórios** (`/relatorios`) com login,
   tema claro/escuro, formato pt-BR e auto-refresh de 60s.

## Pré-requisitos

- **Certificado digital A1 (.pfx)** da empresa.
- **Docker** + **Docker Compose**.

## Setup

```bash
cp .env.example .env
# edite .env: SECRET_KEY, AUTH_*, EMPRESA_*, CERT_PASSWORD, SMTP_*

mkdir -p certs
cp /caminho/do/certificado.pfx certs/certificado.pfx

docker compose up -d --build
```

Acesse `http://localhost:8000` e logue com `AUTH_EMAIL`/`AUTH_PASSWORD`.

## Adicionar uma nova empresa

```bash
cp -r cota-diesel/ cota-diesel-empresa-b/
cd cota-diesel-empresa-b/
# edite .env: EMPRESA_*, AUTH_*, SECRET_KEY, certificado
# troque a porta no docker-compose.yml (ex.: 8001:8000)
mkdir certs && cp /caminho/empresa-b.pfx certs/certificado.pfx
docker compose up -d --build
```

## Variáveis principais (`.env`)

| Var | Descrição |
| --- | --- |
| `EMPRESA_NOME` / `EMPRESA_CNPJ` | Identificação. CNPJ apenas dígitos. |
| `COTA_LITROS` | Litros totais da cota (ex.: `1220000`). |
| `PERIODO_INICIO` / `PERIODO_FIM` | Datas em ISO (`YYYY-MM-DD`). |
| `EMAIL_ALERTAS` | Destinatário dos alertas. |
| `AUTH_EMAIL` / `AUTH_PASSWORD` | Login do painel. |
| `CERT_PATH` / `CERT_PASSWORD` | Certificado A1 (`./certs`). |
| `SEFAZ_AMBIENTE` | `1` produção, `2` homologação. |
| `SEFAZ_UF` | UF do consultante (`cUFAutor`). |
| `MANIFESTAR_AUTO` | `true` para manifestar ciência automaticamente. |
| `POLL_INTERVAL` | Intervalo do polling em segundos (default 900). |
| `MIN_SEFAZ_INTERVAL` | Mínimo entre consultas (anti `cStat 656`). |
| `TZ` | Fuso horário (default `America/Sao_Paulo`). |
| `ALERT_THRESHOLDS` | `70,85,95,100`. |
| `SMTP_*` | Configuração SMTP. |

## Por que manifestação de ciência?

Sem manifestação, o `NFeDistribuicaoDFe` entrega muitas NFe **só como
resumo (resNFe)** — o resumo não tem o detalhamento de itens, então não
dá pra contabilizar litros. A manifestação de ciência (evento 210210)
sinaliza ao SEFAZ que o destinatário tem ciência da operação e libera o
download da NFe completa.

A manifestação tem **efeito jurídico**: você está dando ciência da
operação ao fisco. Não há obrigatoriedade tributária no ato em si, mas
combine com o contador antes de ligar `MANIFESTAR_AUTO=true`. Como
alternativa segura, use o botão “Manifestar” caso a caso.

## Estrutura

```
app/
  main.py              # FastAPI: login, dashboard, relatórios, /sync, /manifestar/{chave}
  worker.py            # APScheduler: polling SEFAZ
  config.py            # Settings (lê .env)
  database.py          # SQLAlchemy + run_migrations() idempotente
  models.py            # NotaFiscal (com is_resumo/cancelada/manifestada), Alerta, State
  auth.py              # Login simples comparando com .env
  format.py            # Filtros Jinja pt-BR + timezone
  services/
    sefaz.py           # Cliente DistribuicaoDFe + RecepcaoEvento4 (mTLS A1)
    eventos.py         # Construção e assinatura XMLDSig do evento 210210
    parser.py          # parse_nfe / parse_resumo / parse_evento
    quota.py           # Soma litros (excluindo resumos e canceladas), alertas
    email.py           # SMTP
    ingest.py          # Orquestração SEFAZ → DB → cota; manifestação opcional
  templates/           # base, login, dashboard, relatorios
  static/app.css       # Tema claro/escuro
docker-compose.yml     # app + worker + postgres
Dockerfile
```

## Limitações conhecidas

- **Janela de ~90 dias do `NFeDistribuicaoDFe`**: NFe muito antigas não
  voltam. Quem precisa de histórico anterior tem que importar XMLs
  manualmente (extensão futura).
- **Throttle do SEFAZ** (`cStat 656` — Consumo Indevido): respeitado via
  `MIN_SEFAZ_INTERVAL` (default 60s).
- **Assinatura de evento**: usa `signxml` com `rsa-sha1` (NT 2014.002).
  Algumas UFs aceitam `sha256`; ajuste em `services/eventos.py` se
  necessário.
- **CCe e demais eventos** (carta de correção etc.) são apenas
  registrados como contador, sem aplicação na cota.

## Segurança

- Sessão via cookie assinado (`itsdangerous`).
- Login comparado com `secrets.compare_digest` contra `.env`.
- `.pfx` montado read-only via volume; nunca vai para a imagem.
- `.env` e `certs/` no `.gitignore`.
