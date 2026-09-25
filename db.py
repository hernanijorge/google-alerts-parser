#!/usr/bin/env python3
"""
Conexão com a Autonomous Database do OCI (GOOGLEALERTS).

O wallet (credenciais mTLS) não é commitado no repositório — é entregue
via variável de ambiente ORACLE_WALLET_BASE64 (conteúdo do .zip do wallet
em Base64) e extraído em disco na primeira conexão.

Variáveis de ambiente esperadas (configuradas no Easypanel):
  ORACLE_WALLET_BASE64  -> conteúdo do Wallet_GOOGLEALERTS.zip em Base64
  ORACLE_WALLET_PASSWORD -> senha definida no download do wallet
  ORACLE_DSN             -> connection string completa (serviço "_tp")
  ORACLE_DB_PASSWORD     -> senha do usuário ADMIN
  ORACLE_DB_USER         -> opcional, default "ADMIN"
"""
import base64
import io
import os
import zipfile

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
