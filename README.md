# google-alerts-parser

Serviço HTTP (FastAPI) que recebe e-mails do Google Alerts (via n8n), extrai os artigos de cada alerta e grava num banco Oracle.

## Arquitetura

```
Google Alerts (e-mail) → Gmail (uma ou mais contas)
    → n8n (Gmail Trigger, polling a cada minuto, hospedado no Easypanel)
    → este serviço, endpoint /ingest (também no Easypanel)
    → Oracle Autonomous Database (schema GOOGLE_ALERTS_OWNER: ALERT_EMAIL / ALERT_ARTICLE)
    → pipelines de enriquecimento (FinBERT, NER, embeddings/dedup — repo separado,
       google-alerts-enrichment-pipelines)
    → Power BI (relatórios — repo separado, kevin-google-alerts-nyt-dashboard)
```

- **Ingestão em tempo real**: n8n monitora a caixa via Gmail Trigger (polling a cada minuto), filtra por assunto (`Google Alert`) e manda o corpo já decodificado do e-mail pro endpoint `/ingest`.
- **Backfill histórico**: um segundo workflow n8n (acionado por webhook, um por conta Gmail) varre o histórico completo da caixa e reenvia cada e-mail pro mesmo `/ingest` — gravação idempotente (dedup por `gmail_message_id` e por `(ALERT_EMAIL_ID, ARTICLE_URL)`), seguro pra rodar (ou re-rodar). O loop Python que orquestra esse backfill (`kevin_backfill_loop.py`/`kjcresearch_backfill_loop.py`) vive no repo `google-alerts-enrichment-pipelines`, pasta `backfill/`.
- Dois formatos de e-mail suportados: "Daily digest" e "As-it-happens" (instantâneo) — em inglês e português (ver seção dedicada abaixo).
- Banco: Oracle Autonomous Database (OCI, tier Always Free), schema dedicado `GOOGLE_ALERTS_OWNER`.

## Deploy do zero no Easypanel

1. **Criar o projeto** no Easypanel (qualquer nome, ex. `google-alerts`).
2. **Adicionar um serviço App** dentro do projeto:
   - **Source**: GitHub, repositório `hernanijorge/google-alerts-parser`, branch `main`.
   - **Build method**: `Dockerfile` (usa o `Dockerfile` na raiz do repo — `python:3.12-slim`, instala `requirements.txt`, copia `parser.py main.py db.py`, expõe a porta `8000`, roda `uvicorn main:app`).
   - **Porta**: `8000` (mapeie pro domínio/proxy que o Easypanel for usar).
3. **Variáveis de ambiente** (configurar na aba Environment do serviço — nomes abaixo, nunca os valores):

   | Variável | Propósito |
   |---|---|
   | `PARSER_API_TOKEN` | Token Bearer exigido em todos os endpoints de escrita (`/parse`, `/parse-text`, `/ingest`, `/db-health`, `/db-stats`, `/reprocess-batch`, `/debug/source-snippet-counts`). Sem essa variável configurada, o serviço nega todas as chamadas autenticadas (falha fechada — ver `check_auth()` em `main.py`). |
   | `ORACLE_WALLET_BASE64` | Conteúdo do `Wallet_<nome>.zip` (baixado no OCI Console, Autonomous Database → DB Connection → Download Wallet) codificado em Base64. Extraído em disco (`/app/wallet`) na primeira conexão. |
   | `ORACLE_WALLET_PASSWORD` | Senha definida no momento do download do wallet. |
   | `ORACLE_DSN` | Connection string completa (alias do `tnsnames.ora` dentro do wallet — use o serviço `_tp` pra throughput balanceado, ou `_high`/`_low` conforme a necessidade). |
   | `ORACLE_DB_USER` | Opcional, default `"ADMIN"`. Em produção, use `GOOGLE_ALERTS_OWNER` (schema de aplicação dedicado — ver SQL abaixo), nunca `ADMIN` fora de manutenção pontual. |
   | `ORACLE_DB_PASSWORD` | Senha do usuário configurado em `ORACLE_DB_USER`. |

4. **Deploy**: o Easypanel builda a imagem a partir do `Dockerfile` e inicia o container. Confirme com `GET /health` (sem autenticação) e depois `GET /db-health` (com `Authorization: Bearer <PARSER_API_TOKEN>`) pra validar que a conexão Oracle está funcionando antes de apontar qualquer workflow n8n pra esse serviço.
5. **Auto-deploy em pushes**: configure o auto-deploy do Easypanel apontando pro mesmo branch (`main`) do repositório GitHub — ver limitação conhecida sobre isso no item (h) abaixo antes de assumir que todo push realmente dispara um novo deploy.

## Endpoints

| Endpoint | Autenticação | Função |
|---|---|---|
| `GET /health` | Nenhuma | Healthcheck público — só confirma que o processo está de pé. |
| `POST /parse` | Bearer | Recebe um `.eml` bruto no corpo (`Content-Type: message/rfc822` ou `application/octet-stream`) e devolve o JSON estruturado do alerta, **sem gravar nada**. Usado pra validar o parser contra uma amostra real antes de confiar em produção. |
| `POST /parse-text` | Bearer | Variante de `/parse` que recebe o corpo do e-mail já decodificado como texto puro (`{"text": "..."}`), sem gravar nada. Existe porque o n8n (Gmail Trigger com "Simplify" desligado) já entrega o texto puro pronto — reconstruir o `.eml` bruto dentro do workflow seria trabalho redundante. |
| `POST /ingest` | Bearer | Parseia **e grava** no banco (`ALERT_EMAIL` + `ALERT_ARTICLE`, numa transação). Endpoint de produção chamado pelo workflow n8n. Se `gmail_message_id` já foi processado, retorna `{"status": "duplicate"}` em vez de erro (protege contra o Gmail Trigger reprocessar a mesma mensagem entre ciclos de poll). |
| `GET /db-health` | Bearer | Testa a conexão com a Autonomous Database (`SELECT 1 FROM DUAL`) — valida wallet + credenciais sem gravar nada. |
| `GET /db-stats` | Bearer | Contagem de linhas (`ALERT_EMAIL`/`ALERT_ARTICLE`) e estimativa de espaço usado (`USER_SEGMENTS`) pelo schema conectado, pra acompanhar o limite de 20GB do Always Free. Não é a medida oficial (essa é o alarme nativo de Storage utilization do OCI), só uma aproximação rápida via API. |
| `POST /reprocess-batch` | Bearer | Reprocessa um lote (até 50) de e-mails já gravados cujo `SOURCE_NAME` ainda está nulo: relê `RAW_TEXT`, re-parseia, e faz `UPDATE` de `TITLE`/`SOURCE_NAME`/`SNIPPET` sem inserir nem remover artigos. Idempotente. Usado pelo backfill retroativo depois de uma correção no parser. |
| `GET /debug/source-snippet-counts` | Bearer | Query de validação: por conta Gmail, quantos artigos têm `SOURCE_NAME`/`SNIPPET` preenchidos — usado pra acompanhar o progresso do reprocessamento retroativo. |

## SQL de criação do schema

Rodar como `ADMIN` (via Database Actions ou outro cliente SQL) no Oracle Autonomous Database.

```sql
-- Sequências/IDs usam GENERATED ALWAYS AS IDENTITY (sem necessidade de sequence manual)
CREATE TABLE GOOGLE_ALERTS_OWNER.ALERT_EMAIL (
    ID                NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    GMAIL_ACCOUNT     VARCHAR2(255) NOT NULL,
    GMAIL_MESSAGE_ID  VARCHAR2(100) NOT NULL,
    TOPIC             VARCHAR2(500) NOT NULL,
    CADENCE_LABEL     VARCHAR2(200),
    CADENCE_DATE_RAW  VARCHAR2(200),
    RECEIVED_AT       TIMESTAMP WITH TIME ZONE,
    RAW_TEXT          CLOB,
    CREATED_AT        TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT UQ_ALERT_EMAIL_MSG_ID UNIQUE (GMAIL_MESSAGE_ID)
);

CREATE INDEX GOOGLE_ALERTS_OWNER.IDX_ALERT_EMAIL_TOPIC ON GOOGLE_ALERTS_OWNER.ALERT_EMAIL(TOPIC);
CREATE INDEX GOOGLE_ALERTS_OWNER.IDX_ALERT_EMAIL_ACCOUNT ON GOOGLE_ALERTS_OWNER.ALERT_EMAIL(GMAIL_ACCOUNT);

CREATE TABLE GOOGLE_ALERTS_OWNER.ALERT_ARTICLE (
    ID                 NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ALERT_EMAIL_ID     NUMBER NOT NULL,
    POSITION_IN_EMAIL  NUMBER,
    TITLE              VARCHAR2(1000),
    SOURCE_NAME        VARCHAR2(200),
    SNIPPET            VARCHAR2(4000),
    ARTICLE_URL        VARCHAR2(4000),
    CREATED_AT         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT FK_ALERT_ARTICLE_EMAIL FOREIGN KEY (ALERT_EMAIL_ID)
        REFERENCES GOOGLE_ALERTS_OWNER.ALERT_EMAIL(ID),
    -- Rede de segurança extra contra o bug de link duplicado (ver histórico abaixo):
    -- mesmo se o parser falhar e repetir um link, o banco rejeita a segunda gravação.
    CONSTRAINT UQ_ALERT_ARTICLE_EMAIL_URL UNIQUE (ALERT_EMAIL_ID, ARTICLE_URL)
);
```

### Usuário de aplicação (schema owner, privilégio mínimo)

`GOOGLE_ALERTS_OWNER` não é um usuário `ADMIN`/DBA — é um schema de aplicação dedicado que só pode criar/alterar objetos dentro do seu próprio schema, sem nenhum privilégio administrativo sobre o banco como um todo:

```sql
CREATE USER GOOGLE_ALERTS_OWNER IDENTIFIED BY "<senha escolhida por você, nunca pelo CLI/IA>";
GRANT CREATE SESSION, CREATE TABLE, CREATE INDEX, CREATE SEQUENCE, CREATE VIEW TO GOOGLE_ALERTS_OWNER;
ALTER USER GOOGLE_ALERTS_OWNER QUOTA UNLIMITED ON DATA;
```

As tabelas acima são criadas *dentro* do schema `GOOGLE_ALERTS_OWNER` (rodando como esse usuário, ou como `ADMIN` especificando o schema). Os pacotes de enriquecimento (repo `google-alerts-enrichment-pipelines`) usam usuários **ainda mais restritos** (`SENTIMENT_WRITER`, `ENTITY_WRITER`, `EMBEDDING_WRITER`), cada um só com `SELECT`/`UPDATE` nas colunas específicas que seu pacote precisa — nunca o `GOOGLE_ALERTS_OWNER` em si, que é reservado pra este serviço de ingestão.

## Formatos de e-mail suportados

| Formato | Idiomas | Detecção | Observações |
|---|---|---|---|
| **Daily digest** (cadência diária/semanal) | Inglês | Marcador "NEWS" + rodapé "See more results / Edit this alert" | Múltiplas notícias por e-mail, cada uma com `title`/`source_name`/`snippet` separados de forma confiável. |
| **As-it-happens** (notificação instantânea) | Inglês e português | Estrutural — cabeçalho `=== <categoria> - <N> new result(s) for [<topic>] ===` (ou equivalente em português, ex. "Notícias - 10 novos resultados para [NYT]"); não depende de palavra-chave de idioma | 1+ resultados por e-mail. `title`/`source_name`/`snippet` são separados por uma heurística de quebra de linha (`_split_instant_block` em `parser.py`) — ver limitação conhecida abaixo. |

### Limitação conhecida

- **Daily digest em português ainda não é suportado** — o filtro de assunto/estrutura desse formato só reconhece o padrão em inglês. "As-it-happens" já suporta português (ver tabela acima).
- Dentro de "As-it-happens", se o próprio nome da fonte quebrar em duas linhas pelo word-wrap do Gmail (ex. "AD HOC\nNEWS"), só a última linha é capturada como `SOURCE_NAME` — limitação da heurística de texto puro, não resolvida (cobre a grande maioria dos casos reais, mas não 100%).

## Bugs já corrigidos e lições

| Bug | Causa | Lição / correção |
|---|---|---|
| **Heurística de rodapé frágil, formato "As-it-happens"** (2 incidentes separados) | (1) Um hífen solto no meio do título de um artigo (quebra de linha do Gmail) foi confundido com o separador de rodapé (`- - - -`), truncando o parsing e descartando 51 links reais de uma vez. (2) A substring solta `"Unsubscribe"` usada como marcador de fim de corpo dava falso positivo quando o próprio snippet de um artigo citava algo como "Subscribe to updates. Unsubscribe from updates." (CTA do site de origem, não do Google) — cortava o corpo **antes de qualquer link**, gerando 422 e descartando o e-mail inteiro. Esse segundo bug, em particular, afetou contas com volume alto de alertas cujo conteúdo citava esse tipo de CTA — truncando sistematicamente parte do histórico até ser corrigido. | Corrigido exigindo 3+ hifens consecutivos pro separador (não 1), e a frase completa `"Unsubscribe from this Google Alert"` pro marcador de rodapé (não a substring solta `"Unsubscribe"`). **Lição**: qualquer heurística de "fim de bloco" baseada em texto solto (não estrutural) tende a colidir com o próprio conteúdo que está tentando delimitar — prefira a frase/token mais específico disponível, mesmo que pareça redundante. |
| **Extração de `SOURCE_NAME`/`SNIPPET` vazia no formato "As-it-happens"** | O parser original assumia que o nome da fonte não aparecia em texto puro nesse formato (só no HTML, sem `href`). Na prática, ele aparece, só que sem posição de linha fixa — o Gmail quebra o texto em ~70-80 colunas, e onde a quebra cai depende do tamanho de cada título. Afetava ~100% dos artigos de uma das contas monitoradas. | `_split_instant_block()`: localiza a primeira linha "longa" (acima de um limiar de caracteres) a partir da segunda linha do bloco — ela marca o início do resumo; a fonte é a cauda de linhas curtas imediatamente antes dela. Reprocessamento retroativo (`/reprocess-batch`) corrigiu o backlog já gravado sem precisar re-ingerir nada do Gmail. |
| **Links duplicados contados em dobro** | Um artigo apareceu com o mesmo link de redirecionamento do Google repetido duas vezes consecutivas no mesmo e-mail. | Corrigido agrupando links consecutivos que apontam pra mesma URL antes de contar como artigos distintos; `UQ_ALERT_ARTICLE_EMAIL_URL` no banco é a rede de segurança caso essa lógica falhe de novo. |
| **Query de reprocessamento degradando o banco** | `get_reparse_candidates()` excluía candidatos "presos" (que nunca resolvem o `SOURCE_NAME` nulo, por limitação conhecida da heurística) via uma lista que só crescia (`NOT IN` com milhares de binds), forçando um hard parse completo a cada chamada — contribuiu pra pressão de armazenamento/CPU sob carga (ver próximo item). | Commit `50ce620`: filtro de "presos" aplicado dentro do próprio SQL em blocos de até 900 IDs (`_EXCLUDE_CHUNK_SIZE`), e a lista de exclusão persistida server-side em vez de recalculada. |
| **Pressão de armazenamento no tier Always Free** | O Autonomous Database Always Free tem um limite físico de 20GB que nunca encolhe automaticamente — confirmado via documentação oficial da Oracle que não existe forma de reduzir esse espaço nesse tier, mesmo depois de liberar linhas. | Mitigado via compressão do texto bruto (~7,7x) e desfragmentação de tabelas, liberando espaço interno reaproveitável. Monitorado via `/db-stats` + alarme nativo do OCI (ver também `storage_stop_threshold_percent` nos loops de backfill, que param automaticamente a 70% de uso como margem antes do alarme real a 75%). |
| **Rotação de senha do usuário `GOOGLE_ALERTS_OWNER`** | A senha do schema de aplicação precisou ser rotacionada em algum momento (prática padrão de segurança / exposição acidental), e isso exige atualizar `ORACLE_DB_PASSWORD` em **todos** os consumidores que conectam como esse usuário — não só este serviço, mas qualquer pipeline que também use esse usuário diretamente. | **Lição**: prefira sempre usuários de aplicação dedicados e restritos por pacote (ver os `*_WRITER` do repo de enriquecimento) em vez de reusar o schema owner em múltiplos lugares — rotacionar a senha de um usuário usado em 1 lugar só é trivial; rotacionar a de um usuário usado em N lugares exige coordenar N atualizações sem deixar nenhuma desatualizada (o que causa falhas de autenticação silenciosas até alguém notar). |
| **Gap no auto-deploy GitHub → Easypanel** | Nem todo push pro branch monitorado necessariamente disparava um novo deploy automaticamente — o gatilho de auto-deploy do Easypanel depende do webhook do GitHub estar corretamente configurado/ativo no repositório, e isso pode ficar fora de sincronia silenciosamente (ex. se o webhook for recriado, ou se o token da integração expirar) sem nenhum erro visível pra quem só olha o código. | **Lição**: depois de qualquer push que deveria mudar o comportamento em produção, **confirme o deploy realmente aconteceu** (horário do último deploy no painel do Easypanel, ou `GET /health`/`GET /db-health` pra validar que o container rodando reflete o código novo) em vez de assumir que o auto-deploy disparou só porque o push foi feito. |

## Projetos relacionados

- [`nyt_news_collector`](https://github.com/hernanijorge/nyt_news_collector) — coleta de artigos do New York Times, mesmo banco Oracle (schema separado).
- [`google-alerts-enrichment-pipelines`](https://github.com/hernanijorge/google-alerts-enrichment-pipelines) — pacotes de enriquecimento (FinBERT, NER, embeddings/dedup) e os loops de backfill, rodando sobre as tabelas criadas por este serviço.
- [`kevin-google-alerts-nyt-dashboard`](https://github.com/hernanijorge/kevin-google-alerts-nyt-dashboard) — o relatório Power BI entregue pro Kevin.
- [`google-alerts-nyt-dashboard-source`](https://github.com/hernanijorge/google-alerts-nyt-dashboard-source) — projeto-fonte (PBIP) do mesmo dashboard, separado do repo acima.
