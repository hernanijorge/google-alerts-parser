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

Autenticação: header "Authorization: Bearer <PARSER_API_TOKEN>".
O token é lido da variável de ambiente PARSER_API_TOKEN (configurada no
Easypanel, nunca commitada no repositório).
"""
import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from parser import parse_eml_bytes, parse_alert_text
from db import get_connection

app = FastAPI(title="Google Alerts Parser", version="0.3.0")


class ParseTextRequest(BaseModel):
    text: str

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


@app.post("/parse-text")
async def parse_text(body: ParseTextRequest, authorization: str | None = Header(default=None)):
    """
    Variante de /parse que recebe o corpo do e-mail já decodificado como
    texto puro (JSON: {"text": "..."}), em vez do .eml bruto.

    Existe porque o n8n (Gmail Trigger com "Simplify" desligado) já entrega
    o campo `text` com o corpo em texto puro pronto (MIME/quoted-printable
    já resolvidos pelo próprio n8n) — reconstruir o .eml bruto dentro do
    workflow seria trabalho redundante.
    """
    check_auth(authorization)

    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Campo 'text' vazio.")

    try:
        result = parse_alert_text(body.text)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return JSONResponse(content=result)
