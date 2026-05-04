# Cota Diesel — Automação de NFe e cota de ICMS

Automação que monitora as notas fiscais de **óleo diesel** emitidas contra um
ou mais CNPJs, soma a galonagem (litros) consumida e dispara alertas por
**email** + **dashboard web** quando a empresa se aproxima do teto da cota
do benefício de ICMS reduzido. Multi-tenant: dá pra acompanhar várias empresas
no mesmo painel.

## Como funciona

1. **Worker** (`app/worker.py`) consulta o serviço da SEFAZ
   **NFeDistribuicaoDFe** a cada `POLL_INTERVAL` segundos, usando certificado
   digital A1 (.pfx) — esse é o webservice oficial pra um destinatário (CNPJ)
   baixar todas as NFe emitidas contra ele.
2. As NFe novas são parseadas (`app/services/parser.py`), filtrando itens de
   diesel pelo NCM (`27101921/22/31`) e palavras-chave no `xProd`.
3. Os litros são somados por empresa dentro do período da cota. Se o consumo
   ultrapassar 70%, 85%, 95% ou 100% (configurável em `ALERT_THRESHOLDS`), um
   email é disparado e o alerta fica registrado pra não duplicar.
4. **Dashboard** (`app/main.py`) exibe cards por empresa com barra de progresso,
   consumo, restante e detalhe das últimas notas. Faz auto-refresh a cada 60s.
5. **Login obrigatório**. Admin cria empresas e usuários; usuário normal só vê
   a empresa vinculada — pronto pra escalar pra várias empresas.

## Pré-requisitos

- **Certificado digital A1 (.pfx)** da empresa (o mesmo usado pra emitir NFe).
- **Docker** + **Docker Compose**.

## Setup

```bash
cp .env.example .env
# edite .env com SECRET_KEY, credenciais SMTP, senha do certificado, etc.

mkdir -p certs
cp /caminho/do/certificado.pfx certs/certificado.pfx

docker compose up -d --build
```

Acesse `http://localhost:8000` e faça login com `ADMIN_EMAIL` / `ADMIN_PASSWORD`
(definidos no `.env`). Em **Admin** cadastre a empresa:

- **CNPJ**: 14 dígitos (sem pontuação ok, é normalizado).
- **Cota (L)**: ex. `1220000` para os 1.220.000 L.
- **Período início/fim**: ex. `2025-01-01` → `2025-06-30` para o semestre.
- **Email alertas**: destinatário dos avisos.

O worker faz a primeira consulta logo no start. Você também pode forçar uma
sincronização clicando em **“Sincronizar com SEFAZ agora”** na página da
empresa.

## Variáveis principais (`.env`)

| Var | Descrição |
| --- | --- |
| `CERT_PATH` / `CERT_PASSWORD` | Certificado A1 (montado em `./certs`). |
| `SEFAZ_AMBIENTE` | `1` produção, `2` homologação. |
| `SEFAZ_UF` | UF do consultante (ex. `SP`). Define `cUFAutor` na consulta. |
| `POLL_INTERVAL` | Intervalo do polling em segundos (default 900 = 15min). |
| `ALERT_THRESHOLDS` | `70,85,95,100` — porcentagens que disparam email. |
| `SMTP_*` | Configuração de envio de email. |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Usuário admin criado no primeiro start. |

## Estrutura

```
app/
  main.py              # FastAPI + rotas + auth + dashboard
  worker.py            # APScheduler: polling periódico
  config.py            # Settings via pydantic-settings
  database.py          # SQLAlchemy engine/session
  models.py            # Empresa / Usuario / NotaFiscal / Alerta
  auth.py              # bcrypt + sessão
  services/
    sefaz.py           # cliente NFeDistribuicaoDFe (SOAP + mTLS A1)
    parser.py          # parse XML NFe + filtro diesel
    quota.py           # soma litros, calcula %, dispara email
    email.py           # SMTP
    ingest.py          # orquestração SEFAZ → DB → cota
  templates/           # Jinja2 (login, dashboard, empresa, admin)
  static/app.css       # estilos
docker-compose.yml     # app + worker + postgres
Dockerfile
```

## Notas técnicas / pontos de extensão

- **`docZip` resumo**: quando o SEFAZ devolve apenas o resumo (`resNFe`), o MVP
  ignora. Pra obter a NFe completa, implemente `consNSU` por chave em
  `services/sefaz.py` ou faça manifestação de ciência antes (evento 210210).
- **Cotas múltiplas / janelas deslizantes**: hoje a cota é uma só por empresa
  com início/fim fixo. Pra rolling 6m, evolua o modelo `Empresa` para guardar
  histórico de cotas/períodos.
- **Filtro de diesel**: ajuste `NCM_DIESEL_PREFIXES` e `DIESEL_KEYWORDS` em
  `services/parser.py` se a sua operação envolve produtos específicos
  (B-S10, ARLA não conta, etc.).
- **Eventos / cancelamento**: o worker grava cada NFe uma vez. Eventos de
  cancelamento ainda não baixam a litragem — é uma extensão recomendada
  (escutar `procEventoNFe` e marcar a nota como cancelada).
- **Escala**: troque o `BlockingScheduler` por Celery + Redis quando o número
  de empresas crescer.

## Segurança

- Sessões com cookie assinado (`itsdangerous`); senhas com `bcrypt`.
- O `.pfx` fica fora da imagem Docker (montado read-only via volume).
- `.env` e `certs/` estão no `.gitignore`.
