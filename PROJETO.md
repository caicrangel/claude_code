# Sistema de Cota de Diesel — Visão Geral do Projeto

## Propósito

Empresas de transporte podem obter, via regime especial de ICMS (às vezes por
decisão judicial/liminar), uma **cota de litros de óleo diesel** com tributação
reduzida por período. Estourar a cota tem consequência financeira direta — e o
controle costuma ser feito na mão, somando notas fiscais em planilha.

Este sistema automatiza esse controle de ponta a ponta:

1. **Busca sozinho, na SEFAZ, toda NFe de diesel emitida contra o CNPJ da empresa** —
   sem depender de ninguém encaminhar XML.
2. **Soma os litros consumidos no período da cota** e mostra o quanto já foi usado.
3. **Avisa por e-mail e Telegram** quando chega nota nova e quando o consumo
   cruza limites de atenção (70%, 85%, 95%, 100%).
4. Oferece **dashboard web com login**, relatórios com gráficos e um PDF de
   panorama mensal.

É **single-tenant**: 1 deploy = 1 empresa. Para outra empresa, sobe-se outra
instância com outro `.env`.

---

## Stack

| Camada | Tecnologia |
| --- | --- |
| Backend / web | Python 3 + **FastAPI** (rotas server-rendered) |
| Templates | **Jinja2** + HTMX (auto-refresh do painel) |
| Banco | **PostgreSQL** (SQLAlchemy 2) |
| Agendador | **APScheduler** rodando em um container separado (worker) |
| Gráficos web | Chart.js |
| PDF | matplotlib (páginas de resumo/panorama) |
| Integração fiscal | Webservices SOAP da SEFAZ com certificado digital A1 (mTLS) + assinatura XML (signxml/lxml) |
| Deploy | Docker Compose com 3 serviços: `db`, `app` (uvicorn) e `worker` |

---

## Como funciona (fluxo principal)

```
        ┌────────────┐   NSU sequencial    ┌──────────────┐
        │   SEFAZ    │◄────────────────────│    worker    │  (a cada 2h)
        │ (DFe/mTLS) │────docZip───────────►│  ingest.py   │
        └────────────┘                     └──────┬───────┘
                                                  │ parse + classifica
                                                  ▼
                                           ┌──────────────┐
                                           │  PostgreSQL  │
                                           └──────┬───────┘
                          ┌───────────────────────┼─────────────────────┐
                          ▼                       ▼                     ▼
                   ┌────────────┐          ┌────────────┐        ┌────────────┐
                   │ dashboard  │          │  alertas   │        │ relatórios │
                   │ (FastAPI)  │          │email+telegr│        │  + PDF     │
                   └────────────┘          └────────────┘        └────────────┘
```

### 1. Ingestão (worker → `services/ingest.py`)

- O **worker** consulta o webservice `NFeDistribuicaoDFe` da SEFAZ em intervalo
  fixo, iterando o **NSU** (número sequencial — o "cursor" da distribuição de
  documentos). O último NSU processado fica salvo na tabela `state`, então o
  sistema retoma de onde parou mesmo após restart.
- Cada documento retornado é classificado pelo schema:
  - **NFe completa (`procNFe`)** → parseada (`services/parser.py`); somente os
    itens de diesel contam litros (NCM `2710.19*` ou keywords DIESEL/S10/S500),
    aceitando as unidades LT, LTS, L, LITRO etc.
  - **Resumo (`resNFe`)** → gravado como pendente; para destravar a NFe
    completa o sistema **manifesta ciência** (evento 210210 assinado) e depois
    baixa o XML completo.
  - **Evento** → cancelamento (110111) marca a nota como cancelada e a tira da
    conta da cota.
- Proteção contra o erro **cStat=656** (consumo indevido): backoff/bloqueio
  temporário e cooldown no botão de sincronização manual.
- Também há **upload manual de XML** no dashboard, para casos raros em que a
  SEFAZ não entregue uma nota.

### 2. Cota e períodos (`services/quota.py`, `services/periodo.py`)

- Um **período de cota** tem início, fim e teto de litros. Um período fica
  **ativo** por vez; os demais viram histórico (consultável nos relatórios).
- Datas e cota são **editáveis** e períodos criados por engano podem ser
  excluídos — importante porque liminares mudam as regras no meio do caminho.
- **Modelo aditivo de pertencimento** (caso liminar): uma NF pertence
  naturalmente ao período que contém sua data de emissão, mas pode ser
  **incluída também em outro período** (tabela `nota_periodo`, muitos-para-
  muitos). Ex.: liminar renova a cota em 09/07 e determina que uma NF de maio
  conte na cota nova — ela passa a contar **nos dois** períodos, sem sair do
  original. A função `cond_pertence_periodo()` centraliza essa regra para todo
  cálculo (KPIs, alertas, relatórios).
- Notas podem ser marcadas como **fora da cota** (ex.: natureza de operação que
  não consome cota) sem sair do histórico.

### 3. Alertas (`services/notify.py`, `services/telegram.py`)

- **Nota nova**: a cada ciclo com NFs novas sai 1 e-mail/mensagem agrupada com
  o detalhamento do lote, situação da cota e o bloco **"Compras acumuladas do
  mês — nota a nota"** (cada NF do mês vigente com litros, valor e R$/L, mais
  o total). O acumulado respeita o período ativo e zera na virada do mês.
- **Limites**: ao cruzar 70/85/95/100% da cota dispara alerta dedicado, com
  deduplicação por limite+período (não repete o mesmo aviso).
- **Panorama mensal**: dia 1 de cada mês, e-mail de fechamento com PDF anexo
  (gráficos por fornecedor e consumo mensal).
- **Canais**: e-mail (SMTP configurável, logo embutida via CID) e **Telegram**
  (bot próprio; token criptografado no banco). Destinatários são geridos na
  tela de configuração.
- **Central de disparos** (admin): reenvia manualmente qualquer alerta —
  situação da cota, panorama ou o alerta de NFs específicas — escolhendo os
  canais. Útil quando um alerta saiu com dado errado e precisa ser reemitido.

### 4. Interface web (`main.py` + `templates/`)

- **Login** com sessão assinada; papéis **admin** e **usuário**. Após logout,
  headers `Cache-Control: no-store` + guarda de bfcache impedem que o botão
  "voltar" exiba páginas antigas.
- **Visão geral**: medidor radial do uso da cota, KPIs (consumido/cota/
  restante), tabela das notas do período (litros, valor, R$/L, status),
  sincronização manual e upload de XML.
- **Relatórios**: filtro por período histórico e intervalo de datas, KPIs,
  projeção de fechamento (ritmo diário → estouro previsto), gráficos (top
  fornecedores, consumo mensal, NCM) e tabelas detalhadas.
- **Configuração** (admin): períodos de cota, parâmetros da empresa, SEFAZ e
  certificado A1, SMTP, Telegram, usuários, destinatários de alerta e a
  central de disparos.
- Tema claro/escuro, sidebar recolhível, layout responsivo, formatação pt-BR.

---

## Estrutura de pastas

```
.
├── docker-compose.yml        # db (Postgres) + app (uvicorn) + worker
├── Dockerfile
├── requirements.txt
├── certs/                    # certificado A1 (.pfx) montado no container
└── app/
    ├── main.py               # rotas FastAPI (dashboard, relatórios, config…)
    ├── worker.py             # APScheduler: polling SEFAZ + panorama mensal
    ├── models.py             # tabelas (NotaFiscal, CotaPeriodo, nota_periodo,
    │                         #  usuários, e-mails de alerta, state…)
    ├── database.py           # engine + migrações idempotentes no startup
    ├── auth.py               # sessão, require_login / require_admin
    ├── config.py             # settings do .env (pydantic-settings)
    ├── runtime_config.py     # config editável pela UI, salva no banco
    │                         #  (segredos criptografados — crypto.py)
    ├── format.py             # filtros pt-BR (litros, moeda, datas, %)
    ├── services/
    │   ├── sefaz.py          # SOAP DistribuicaoDFe/RecepcaoEvento, mTLS, XMLDSig
    │   ├── ingest.py         # ciclo de ingestão: NSU, classificação, eventos
    │   ├── parser.py         # extração de litros/valores do XML da NFe
    │   ├── quota.py          # consumo, pertencimento a período, alertas de limite
    │   ├── periodo.py        # CRUD e ativação de períodos de cota
    │   ├── notify.py         # e-mails/telegram de NF nova, situação, panorama
    │   ├── telegram.py       # cliente do bot (sendMessage/sendDocument)
    │   ├── email.py          # SMTP com HTML + imagens inline
    │   ├── logo_email.py     # logo da empresa embutida via CID
    │   └── report_pdf.py     # PDF de panorama (matplotlib)
    ├── templates/            # base, login, dashboard, relatorios, configuracao
    └── static/               # app.css (tema Navy Pro claro/escuro) + logos
```

---

## Decisões de projeto que valem destacar

- **Server-rendered + HTMX** em vez de SPA: o app é essencialmente um painel
  de leitura com formulários simples; Jinja2 mantém tudo em um só lugar e o
  HTMX dá o auto-refresh de 60s sem JavaScript pesado.
- **Worker separado do web**: o polling da SEFAZ é lento e não pode bloquear
  requisições; os dois containers compartilham o mesmo código e banco.
- **Estado no banco, não em memória** (tabela `state` + config em runtime):
  containers podem reiniciar à vontade sem perder o cursor NSU, dedup de
  alertas ou configurações feitas pela UI.
- **Migrações idempotentes no startup** (ADD COLUMN/TABLE IF NOT EXISTS):
  simplicidade de deploy — subir a versão nova já ajusta o schema.
- **Regra de pertencimento centralizada** (`cond_pertence_periodo`): qualquer
  número exibido (KPI, alerta, relatório, acumulado do mês) sai da mesma
  condição SQL, então dashboard e e-mails nunca divergem.

## Como rodar

```bash
cp .env.example .env       # editar: SECRET_KEY, EMPRESA_*, SMTP_*, CERT_PASSWORD
mkdir -p certs && cp certificado.pfx certs/
docker compose up -d --build
# app em http://localhost:8000 — login definido no .env / tela de usuários
```

> Importante: templates e código são copiados para a imagem no build — após um
> `git pull`, sempre `docker compose up -d --build` (restart não basta).
