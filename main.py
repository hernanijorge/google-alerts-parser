#!/usr/bin/env python3
"""
Serviço HTTP do parser de Google Alerts — mesmo padrão do coletor do NYT
(FastAPI + endpoint protegido por token).

Endpoints:
  GET  /health              -> healthcheck simples, sem autenticação
  POST /parse                -> recebe o .eml cru no corpo da requisição
                                 (Content-Type: message/rfc822 ou
                                 application/octet-stream) e devolve o
                                 JSON estruturado do alerta.
  GET  /db-health            -> testa a conexão com a Autonomous Database.
  POST /parse-text           -> recebe o texto já decodificado (JSON) e só
                                 parseia, sem gravar nada (teste/validação).
  POST /ingest                -> parseia E grava no banco (produção).

Autenticação: header "Authorization: Bearer <PARSER_API_TOKEN>".
O token é lido da variável de ambiente PARSER_API_TOKEN (configurada no
Easypanel, nunca commitada no repositório).
"""
import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from parser import parse_eml_bytes, parse_alert_text
from db import (
    get_connection,
    get_storage_stats,
    insert_alert,
    get_raw_samples,
    get_reparse_candidates,
    reprocess_email_articles,
    get_source_snippet_counts,
)

app = FastAPI(title="Google Alerts Parser", version="0.4.0")


class ParseTextRequest(BaseModel):
    text: str


class IngestRequest(BaseModel):
    text: str
    gmail_account: str
    gmail_message_id: str
    received_at: str | None = None


API_TOKEN = os.environ.get("PARSER_API_TOKEN")


def check_auth(authorization: str | None) -> None:
    if not API_TOKEN:
        # Sem token configurado no ambiente = serviço mal configurado.
        # Falha fechada (nega tudo) em vez de deixar aberto sem querer.
        raise HTTPException(status_code=500, detail="PARSER_API_TOKEN não configurado no servidor.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Header Authorization ausente ou inválido.")
    token = authorization.removeprefix("Bearer ").strip()
    if token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido.")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/parse")
async def parse(request: Request, authorization: str | None = Header(default=None)):
    check_auth(authorization)

    raw_bytes = await request.body()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Corpo da requisição vazio — esperado o .eml cru.")

    try:
        result = parse_eml_bytes(raw_bytes)
    except ValueError as e:
        # Erro esperado de parsing (layout inesperado, etc.) -> 422, não 500
        raise HTTPException(status_code=422, detail=str(e))

    return JSONResponse(content=result)


@app.get("/db-health")
def db_health(authorization: str | None = Header(default=None)):
    """
    Testa a conexão com a Autonomous Database (wallet + credenciais).
    Não grava nada — só valida que a conexão mTLS funciona, do mesmo jeito
    que /health valida o serviço em si e /parse validou o parser antes de
    plugar no n8n.
    """
    check_auth(authorization)
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM DUAL")
        (result,) = cursor.fetchone()
        conn.close()
        return {"status": "ok", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha na conexão com o banco: {e}")


@app.get("/db-stats")
def db_stats(authorization: str | None = Header(default=None)):
    """
    Contagem de registros e estimativa de espaço usado pela aplicação, pra
    acompanhar o consumo do limite Always Free (20 GB) da Autonomous Database.
    """
    check_auth(authorization)
    try:
        return get_storage_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao consultar estatísticas do banco: {e}")


@app.post("/parse-text")
async def parse_text(body: ParseTextRequest, authorization: str | None = Header(default=None)):
    """
    Variante de /parse que recebe o corpo do e-mail já decodificado como
    texto puro (JSON: {"text": "..."}), em vez do .eml bruto.

    Existe porque o n8n (Gmail Trigger com "Simplify" desligado) já entrega
    o campo `text` com o corpo em texto puro pronto (MIME/quoted-printable
    já resolvidos pelo próprio n8n) — reconstruir o .eml bruto dentro do
    workflow seria trabalho redundante. Só parseia, não grava nada — serve
    pra teste/validação isolada.
    """
    check_auth(authorization)

    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Campo 'text' vazio.")

    try:
        result = parse_alert_text(body.text)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return JSONResponse(content=result)


@app.get("/debug/sample-raw")
def debug_sample_raw(
    account: str | None = None,
    limit: int = 20,
    offset: int = 0,
    authorization: str | None = Header(default=None),
):
    """
    Leitura pura de RAW_TEXT já armazenado (não grava nada), pra montar
    amostra de teste offline do parser. `limit` limitado a 100 por chamada
    pra não puxar CLOBs grandes demais de uma vez.
    """
    check_auth(authorization)
    limit = max(1, min(limit, 100))
    try:
        return {"samples": get_raw_samples(account, limit, offset)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao consultar amostras: {e}")


@app.get("/debug/source-snippet-counts")
def debug_source_snippet_counts(authorization: str | None = Header(default=None)):
    """Query de validação: por conta, quantos artigos têm SOURCE_NAME/SNIPPET preenchidos."""
    check_auth(authorization)
    try:
        return {"counts": get_source_snippet_counts()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao consultar contagens: {e}")


class ReprocessBatchRequest(BaseModel):
    gmail_account: str | None = None
    limit: int = 20


@app.post("/reprocess-batch")
def reprocess_batch(body: ReprocessBatchRequest, authorization: str | None = Header(default=None)):
    """
    Reprocessa um lote de e-mails já gravados (RAW_TEXT -> novo parse ->
    UPDATE de TITLE/SOURCE_NAME/SNIPPET em ALERT_ARTICLE), sem tocar no
    Gmail. Idempotente: rodar de novo sobre o mesmo registro só sobrescreve
    com o mesmo resultado. Limitado a 50 por chamada pra manter cada
    requisição rápida e não competir demais com a carga do backfill.
    """
    check_auth(authorization)
    limit = max(1, min(body.limit, 50))
    try:
        candidates = get_reparse_candidates(body.gmail_account, limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao buscar candidatos: {e}")

    results = []
    for candidate in candidates:
        raw_text = candidate["raw_text"]
        if not raw_text:
            results.append({"id": candidate["id"], "status": "skipped_empty_raw_text"})
            continue
        try:
            parsed = parse_alert_text(raw_text)
        except ValueError as e:
            results.append({"id": candidate["id"], "status": "parse_error", "detail": str(e)})
            continue
        try:
            result = reprocess_email_articles(candidate["id"], parsed)
        except Exception as e:
            results.append({"id": candidate["id"], "status": "db_error", "detail": str(e)})
            continue
        results.append(result)

    return {"processed": len(results), "remaining_hint": len(candidates) == limit, "results": results}


@app.post("/ingest")
async def ingest(body: IngestRequest, authorization: str | None = Header(default=None)):
    """
    Parseia e GRAVA no banco (ALERT_EMAIL + ALERT_ARTICLE). É o endpoint de
    produção pro workflow do n8n — o /parse-text continua existindo só
    pra teste/validação isolada do parser, sem persistir nada.

    Dedup: se `gmail_message_id` já foi processado antes, não grava de novo
    e retorna status "duplicate" (não é erro) — protege contra o Gmail
    Trigger reprocessar o mesmo e-mail entre ciclos de poll.
    """
    check_auth(authorization)

    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Campo 'text' vazio.")

    try:
        parsed = parse_alert_text(body.text)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        result = insert_alert(
            gmail_account=body.gmail_account,
            gmail_message_id=body.gmail_message_id,
            received_at_raw=body.received_at,
            raw_text=body.text,
            parsed=parsed,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao gravar no banco: {e}")

    return JSONResponse(content=result)
