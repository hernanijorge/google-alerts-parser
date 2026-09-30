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


def get_raw_samples(gmail_account: Optional[str], limit: int, offset: int) -> list[dict]:
    """
    Leitura pura (sem gravar nada) de e-mails já armazenados, pra montar
    uma amostra de teste offline do parser contra RAW_TEXT real. Usado
    pelo endpoint de debug /debug/sample-raw.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if gmail_account:
            cursor.execute(
                """
                SELECT ID, GMAIL_ACCOUNT, TOPIC, CADENCE_LABEL, RAW_TEXT
                FROM ALERT_EMAIL
                WHERE GMAIL_ACCOUNT = :gmail_account
                ORDER BY ID
                OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
                """,
                {"gmail_account": gmail_account, "offset": offset, "limit": limit},
            )
        else:
            cursor.execute(
                """
                SELECT ID, GMAIL_ACCOUNT, TOPIC, CADENCE_LABEL, RAW_TEXT
                FROM ALERT_EMAIL
                ORDER BY ID
                OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
                """,
                {"offset": offset, "limit": limit},
            )
        rows = cursor.fetchall()
        results = []
        for row_id, account, topic, cadence_label, raw_text_lob in rows:
            raw_text = raw_text_lob.read() if raw_text_lob is not None else None
            results.append(
                {
                    "id": row_id,
                    "gmail_account": account,
                    "topic": topic,
                    "cadence_label": cadence_label,
                    "raw_text": raw_text,
                }
            )
        return results
    finally:
        conn.close()


def get_reparse_candidates(gmail_account: Optional[str], limit: int) -> list[dict]:
    """
    Seleciona e-mails que têm pelo menos um artigo com SOURCE_NAME nulo
    (candidatos a reprocessamento retroativo), trazendo o RAW_TEXT pra
    re-parsear localmente. Usado pelo /reprocess-batch.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        base_query = """
            SELECT ae.ID, ae.GMAIL_ACCOUNT, ae.RAW_TEXT
            FROM ALERT_EMAIL ae
            WHERE EXISTS (
                SELECT 1 FROM ALERT_ARTICLE aa
                WHERE aa.ALERT_EMAIL_ID = ae.ID AND aa.SOURCE_NAME IS NULL
            )
        """
        params = {"limit": limit}
        if gmail_account:
            base_query += " AND ae.GMAIL_ACCOUNT = :gmail_account"
            params["gmail_account"] = gmail_account
        base_query += " ORDER BY ae.ID FETCH FIRST :limit ROWS ONLY"

        cursor.execute(base_query, params)
        rows = cursor.fetchall()
        results = []
        for row_id, account, raw_text_lob in rows:
            raw_text = raw_text_lob.read() if raw_text_lob is not None else None
            results.append({"id": row_id, "gmail_account": account, "raw_text": raw_text})
        return results
    finally:
        conn.close()


def reprocess_email_articles(alert_email_id: int, parsed: dict) -> dict:
    """
    Re-grava TITLE/SOURCE_NAME/SNIPPET dos artigos de um e-mail já
    existente, casando por POSITION_IN_EMAIL (a ordem dos artigos no
    e-mail não muda entre parses -- só a qualidade da extração melhora).
    Não insere nem remove artigos, só faz UPDATE dos 3 campos de texto.
    Se o novo parse encontrar um número de artigos diferente do que já
    está gravado (não deveria acontecer, já que a extração de URL não
    mudou), não faz nada e devolve status "skipped_count_mismatch" --
    mais seguro que gravar dado desalinhado.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM ALERT_ARTICLE WHERE ALERT_EMAIL_ID = :id",
            {"id": alert_email_id},
        )
        (existing_count,) = cursor.fetchone()

        articles = parsed.get("articles", [])
        if existing_count != len(articles):
            return {
                "id": alert_email_id,
                "status": "skipped_count_mismatch",
                "existing_count": existing_count,
                "new_count": len(articles),
            }

        updated = 0
        for position, article in enumerate(articles, start=1):
            cursor.execute(
                """
                UPDATE ALERT_ARTICLE
                SET TITLE = :title, SOURCE_NAME = :source_name, SNIPPET = :snippet
                WHERE ALERT_EMAIL_ID = :alert_email_id AND POSITION_IN_EMAIL = :position
                """,
                {
                    "title": article.get("title"),
                    "source_name": article.get("source_name") or None,
                    "snippet": article.get("snippet") or None,
                    "alert_email_id": alert_email_id,
                    "position": position,
                },
            )
            updated += cursor.rowcount

        conn.commit()
        return {"id": alert_email_id, "status": "ok", "articles_updated": updated}
    finally:
        conn.close()


def get_source_snippet_counts() -> list[dict]:
    """Query de validação: por conta, quantos artigos têm SOURCE_NAME/SNIPPET preenchidos."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT ae.GMAIL_ACCOUNT,
                   COUNT(*) AS total_artigos,
                   SUM(CASE WHEN aa.SOURCE_NAME IS NOT NULL THEN 1 ELSE 0 END) AS com_source_name,
                   SUM(CASE WHEN aa.SNIPPET IS NOT NULL THEN 1 ELSE 0 END) AS com_snippet
            FROM ALERT_EMAIL ae
            JOIN ALERT_ARTICLE aa ON aa.ALERT_EMAIL_ID = ae.ID
            GROUP BY ae.GMAIL_ACCOUNT
            ORDER BY ae.GMAIL_ACCOUNT
            """
        )
        cols = ["gmail_account", "total_artigos", "com_source_name", "com_snippet"]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_storage_stats() -> dict:
    """
    Estimativa do espaço usado pela aplicação (schema do usuário conectado,
    GOOGLE_ALERTS_OWNER em produção). USER_SEGMENTS cobre tabelas, índices e
    CLOBs — boa aproximação, mas não é o total exato do banco; o alarme de
    Storage utilization do OCI é a medida oficial.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT USER FROM DUAL")
        connected_as = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM ALERT_EMAIL")
        email_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM ALERT_ARTICLE")
        article_count = cursor.fetchone()[0]
        cursor.execute("SELECT NVL(SUM(BYTES), 0) FROM USER_SEGMENTS")
        total_bytes = cursor.fetchone()[0] or 0
        gb = total_bytes / (1024 ** 3)
        return {
            "connected_as": connected_as,
            "alert_email_count": email_count,
            "alert_article_count": article_count,
            "app_schema_mb": round(total_bytes / (1024 ** 2), 2),
            "app_schema_gb": round(gb, 4),
            "always_free_limit_gb": 20,
            "percent_of_limit": round((gb / 20) * 100, 2),
        }
    finally:
        conn.close()
