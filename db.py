#!/usr/bin/env python3
"""
Conexão com a Autonomous Database do OCI (GOOGLEALERTS).

O wallet (credenciais mTLS) não é commitado no repositório — é entregue
via variável de ambiente ORACLE_WALLET_BASE64 (conteúdo do .zip do wallet
em Base64) e extraído em disco na primeira conexão.

Variáveis de ambiente esperadas (configuradas no Easypanel):
  ORACLE_WALLET_BASE64   -> conteúdo do Wallet_GOOGLEALERTS.zip em Base64
  ORACLE_WALLET_PASSWORD -> senha definida no download do wallet
  ORACLE_DSN             -> connection string completa (serviço "_tp")
  ORACLE_DB_PASSWORD     -> senha do usuário de aplicação (GOOGLE_ALERTS_OWNER)
  ORACLE_DB_USER         -> opcional, default "ADMIN" (produção usa
                             GOOGLE_ALERTS_OWNER, criado com privilégio
                             mínimo — ver doc do projeto)

Tabelas (schema GOOGLE_ALERTS_OWNER, criadas manualmente via Database Actions):
  ALERT_EMAIL   -> um registro por e-mail de alerta processado.
                   Dedup via UNIQUE(GMAIL_MESSAGE_ID): reprocessar o mesmo
                   e-mail (ex.: Gmail Trigger disparando de novo sobre a
                   mesma mensagem) não duplica, só é ignorado.
  ALERT_ARTICLE -> um registro por artigo dentro do e-mail, FK pra ALERT_EMAIL.
                   UNIQUE(ALERT_EMAIL_ID, ARTICLE_URL) como rede de segurança
                   extra contra o bug de link duplicado já visto no parser.
"""
import base64
import io
import os
import zipfile
from datetime import datetime
from typing import Optional

import oracledb

WALLET_DIR = "/app/wallet"


def _ensure_wallet_extracted() -> None:
    if os.path.isdir(WALLET_DIR) and os.listdir(WALLET_DIR):
        return

    wallet_b64 = os.environ.get("ORACLE_WALLET_BASE64")
    if not wallet_b64:
        raise RuntimeError("ORACLE_WALLET_BASE64 não configurado no ambiente.")

    os.makedirs(WALLET_DIR, exist_ok=True)
    zip_bytes = base64.b64decode(wallet_b64)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(WALLET_DIR)


def get_connection() -> oracledb.Connection:
    _ensure_wallet_extracted()

    dsn = os.environ["ORACLE_DSN"]
    user = os.environ.get("ORACLE_DB_USER", "ADMIN")
    password = os.environ["ORACLE_DB_PASSWORD"]
    wallet_password = os.environ["ORACLE_WALLET_PASSWORD"]

    return oracledb.connect(
        user=user,
        password=password,
        dsn=dsn,
        wallet_location=WALLET_DIR,
        wallet_password=wallet_password,
    )


def _parse_received_at(received_at_raw: Optional[str]):
    """
    O n8n manda a data do e-mail (campo `date` do Gmail Trigger) como string
    ISO 8601. Se por algum motivo vier em outro formato ou vazio, não
    trava a gravação — só grava NULL nesse campo (não é dado crítico pro
    dedup, que já é feito pelo GMAIL_MESSAGE_ID).
    """
    if not received_at_raw:
        return None
    try:
        return datetime.fromisoformat(received_at_raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def insert_alert(
    gmail_account: str,
    gmail_message_id: str,
    received_at_raw: Optional[str],
    raw_text: str,
    parsed: dict,
) -> dict:
    """
    Grava um alerta parseado (ALERT_EMAIL + ALERT_ARTICLE em uma transação).

    Se `gmail_message_id` já existir (reprocessamento do mesmo e-mail pelo
    Gmail Trigger), não grava de novo — retorna status "duplicate" em vez
    de estourar erro, pra n8n não precisar tratar isso como falha.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        id_var = cursor.var(oracledb.NUMBER)
        try:
            cursor.execute(
                """
                INSERT INTO ALERT_EMAIL
                    (GMAIL_ACCOUNT, GMAIL_MESSAGE_ID, TOPIC, CADENCE_LABEL,
                     CADENCE_DATE_RAW, RECEIVED_AT, RAW_TEXT)
                VALUES
                    (:gmail_account, :gmail_message_id, :topic, :cadence_label,
                     :cadence_date_raw, :received_at, :raw_text)
                RETURNING ID INTO :id
                """,
                {
                    "gmail_account": gmail_account,
                    "gmail_message_id": gmail_message_id,
                    "topic": parsed.get("topic"),
                    "cadence_label": parsed.get("cadence_label"),
                    "cadence_date_raw": parsed.get("cadence_date_raw"),
                    "received_at": _parse_received_at(received_at_raw),
                    "raw_text": raw_text,
                    "id": id_var,
                },
            )
        except oracledb.IntegrityError:
            conn.rollback()
            return {"status": "duplicate", "gmail_message_id": gmail_message_id}

        alert_email_id = int(id_var.getvalue()[0])

        articles = parsed.get("articles", [])
        for position, article in enumerate(articles, start=1):
            cursor.execute(
                """
                INSERT INTO ALERT_ARTICLE
                    (ALERT_EMAIL_ID, POSITION_IN_EMAIL, TITLE, SOURCE_NAME,
                     SNIPPET, ARTICLE_URL)
                VALUES
                    (:alert_email_id, :position, :title, :source_name,
                     :snippet, :article_url)
                """,
                {
                    "alert_email_id": alert_email_id,
                    "position": position,
                    "title": article.get("title"),
                    "source_name": article.get("source_name"),
                    "snippet": article.get("snippet"),
                    "article_url": article.get("url"),
                },
            )

        conn.commit()
        return {
            "status": "ok",
            "alert_email_id": alert_email_id,
            "articles_inserted": len(articles),
        }
    finally:
        conn.close()
