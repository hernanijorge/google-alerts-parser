# google-alerts-parser

Serviço HTTP (FastAPI) que recebe e-mails do Google Alerts (via n8n), extrai os artigos de cada alerta e grava num banco Oracle.

## Visão geral do pipeline

Google Alerts (e-mail) → Gmail (uma ou mais contas) → n8n (workflow, hospedado no Easypanel) → este serviço (/ingest) → Oracle Autonomous Database (GOOGLE_ALERTS_OWNER.ALERT_EMAIL / ALERT_ARTICLE) → enriquecimento de sentimento (FinBERT) → Power BI (relatórios)

- Ingestão em tempo real: n8n monitora a caixa via Gmail Trigger (polling a cada minuto), filtra por assunto (`Google Alert`) e manda o corpo já decodificado do e-mail pro endpoint `/ingest`.
- Backfill histórico: um segundo workflow (acionado por webhook) varre o histórico completo da caixa e reenvia cada e-mail pro mesmo `/ingest` — gravação idempotente (dedup por `gmail_message_id` e por `(email, url)`), seguro pra rodar (ou re-rodar).
- Dois formatos de e-mail suportados: "Daily digest" e "As-it-happens" (instantâneo) — em inglês e português.
- Banco: Oracle Autonomous Database (OCI, tier Always Free), schema dedicado `GOOGLE_ALERTS_OWNER`, usuário de aplicação de privilégio mínimo.
- Enriquecimento de sentimento: um pipeline separado roda um modelo de IA pré-treinado (FinBERT) sobre os artigos já coletados, adicionando um score de sentimento, sem precisar de dado novo.

## Endpoints principais

| Endpoint | Função |
|---|---|
| `GET /health` | healthcheck público |
| `POST /parse` | parseia um `.eml` bruto (teste/validação) |
| `POST /parse-text` | parseia texto já decodificado, sem gravar (teste) |
| `POST /ingest` | parseia **e** grava no banco — produção |
| `GET /db-health` | testa a conexão com o Oracle |
| `GET /db-stats` | contagem de linhas e uso de armazenamento do schema |
| `POST /reprocess-batch` | reprocessa e-mails que falharam na primeira tentativa |

Todos os endpoints de escrita exigem `Authorization: Bearer <token>`.

## O que já foi construído

- Parser validado contra amostras reais de e-mail antes de montar qualquer infraestrutura.
- Suporte aos dois formatos de e-mail do Google Alerts, incluindo variações de idioma (inglês/português) e desduplicação de links repetidos dentro do mesmo e-mail.
- Serviço FastAPI containerizado no Easypanel, com autenticação por token em todos os endpoints de escrita.
- Workflow de ingestão em tempo real no n8n (Gmail Trigger → HTTP Request → este serviço) e workflow de backfill (via webhook) pra importar o histórico completo de cada conta.
- Esquema de banco com deduplicação em dois níveis, usuário de aplicação de privilégio mínimo, e monitoramento de armazenamento (alarme nativo do OCI + endpoint próprio `/db-stats`).
- Suporte a múltiplas contas Gmail, cada uma com sua própria credencial OAuth2.
- Pipeline de enriquecimento de sentimento (FinBERT), com views de agregação no banco pra manter os relatórios rápidos.

## Problemas encontrados e resolvidos (histórico)

- **Links duplicados**: um artigo apareceu com o mesmo link de redirecionamento repetido duas vezes no mesmo e-mail, contando em dobro — corrigido agrupando links consecutivos que apontam pra mesma URL.
- **Heurística de rodapé frágil**: um hífen solto no meio do título de um artigo (quebra de linha do Gmail) foi confundido com o separador de rodapé, truncando o parsing e descartando 51 links reais. Corrigido exigindo 3+ hifens consecutivos, mais marcadores de rodapé adicionais.
- **Query de reprocessamento degradando o banco**: uma consulta usava uma lista que só crescia, forçando um parse completo a cada execução — contribuiu pra pressão de armazenamento sob carga. Identificada, pausada, e uma correção definitiva (coluna indexada) documentada.
- **Pressão de armazenamento no tier Always Free**: o Autonomous Database Always Free tem um limite físico de 20GB que nunca encolhe automaticamente — confirmado via documentação oficial da Oracle que não existe forma de reduzir esse espaço nesse tier. Mitigado via compressão do texto bruto (~7,7x) e desfragmentação de tabelas, liberando espaço interno reaproveitável.
- **Expiração de autorização OAuth**: a credencial do Gmail de uma conta monitorada expirou, exigindo reautorização manual.
- **Segurança**: o webhook de backfill e o token do parser passaram por correção — autenticação adicionada ao webhook, e o token do parser movido de texto puro (dentro dos workflows do n8n) para uma credential gerenciada pelo próprio n8n.

## Limitações conhecidas

- Banco Oracle no tier Always Free: 1 OCPU, sem auto-scaling, teto de 20GB que não pode ser reduzido depois de alocado.
- Alertas em português no formato "Daily digest" ainda não são suportados (filtro de assunto só em inglês); "As-it-happens" já suporta português.

## Projetos relacionados

- [`nyt_news_collector`](https://github.com/hernanijorge/nyt_news_collector) — coleta de artigos do New York Times, mesmo banco Oracle (schema separado).
