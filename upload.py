"""
Lambda: upload.py
Endpoint: POST /upload

Responsabilidades:
  1. Validar autenticação (user_id via query param; futuro: Cognito JWT)
  2. Decodificar o arquivo do body
  3. Salvar o arquivo no bucket de documentos:
       documentos/users/{userId}/{timestamp}_{filename}
  4. Criar JSON de metadados no bucket de metadados:
       metadata/users/{userId}/{timestamp}_{filename}.json
  5. Retornar o arquivo_id (chave S3) ao cliente
"""
import json
import base64
import os
import uuid
import boto3
from datetime import datetime, timezone

s3 = boto3.client("s3")

DOCS_BUCKET     = os.environ["DOCS_BUCKET"]
METADATA_BUCKET = os.environ["METADATA_BUCKET"]


def handler(event, context):
    try:
        # ── 1. Extrair user_id ──────────────────────────────────────
        # Versão atual: query param
        # Versão futura com Cognito:
        #   user_id = event["requestContext"]["authorizer"]["claims"]["sub"]
        query = event.get("queryStringParameters") or {}
        user_id = query.get("user_id", "").strip()
        if not user_id:
            return error_response(400, "Parâmetro 'user_id' é obrigatório.")

        # ── 2. Decodificar arquivo do body ──────────────────────────
        body = event.get("body", "")
        is_base64 = event.get("isBase64Encoded", False)
        if is_base64:
            file_content = base64.b64decode(body)
        else:
            file_content = body.encode("utf-8") if body else b""

        if not file_content:
            return error_response(400, "Nenhum arquivo enviado no body.")

        # ── 3. Obter nome e tipo do arquivo ─────────────────────────
        headers   = event.get("headers") or {}
        file_name = query.get("filename") or headers.get("x-filename", "documento.pdf")
        file_name = "".join(c for c in file_name if c.isalnum() or c in "._-")
        if not file_name:
            file_name = "documento.pdf"

        content_type = headers.get("content-type", "application/octet-stream")

        # ── 4. Montar chaves S3 ─────────────────────────────────────
        timestamp  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        doc_key    = f"documentos/users/{user_id}/{timestamp}_{file_name}"
        meta_key   = f"metadata/users/{user_id}/{timestamp}_{file_name}.json"
        doc_id     = str(uuid.uuid4())

        # ── 5. Salvar arquivo no bucket de documentos ───────────────
        s3.put_object(
            Bucket=DOCS_BUCKET,
            Key=doc_key,
            Body=file_content,
            ContentType=content_type,
            Metadata={
                "user-id":    user_id,
                "doc-id":     doc_id,
                "original-filename": file_name,
            },
        )

        # ── 6. Criar JSON de metadados ──────────────────────────────
        # O processor.py vai atualizar este JSON com os resultados
        # do Macie e do Bedrock após o processamento.
        metadata = {
            "doc_id":            doc_id,
            "user_id":           user_id,
            "filename":          file_name,
            "s3_key":            doc_key,
            "metadata_key":      meta_key,
            "bucket":            DOCS_BUCKET,
            "upload_timestamp":  datetime.now(timezone.utc).isoformat(),
            "content_type":      content_type,
            "status":            "uploaded",   # processor vai mudar para "processed"
            "macie_result":      None,         # preenchido pelo processor
            "bedrock_summary":   None,         # preenchido pelo processor
        }

        s3.put_object(
            Bucket=METADATA_BUCKET,
            Key=meta_key,
            Body=json.dumps(metadata, ensure_ascii=False, indent=2),
            ContentType="application/json",
        )

        print(f"[UPLOAD] user={user_id} arquivo={doc_key}")

        return {
            "statusCode": 200,
            "headers": cors_headers(),
            "body": json.dumps({
                "message":    "Arquivo enviado com sucesso.",
                "arquivo_id": doc_key,
                "doc_id":     doc_id,
                "filename":   file_name,
                "timestamp":  timestamp,
            }),
        }

    except Exception as e:
        print(f"[ERRO] upload: {str(e)}")
        return error_response(500, "Erro interno ao processar o upload.")


def cors_headers():
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
    }


def error_response(status_code: int, message: str):
    return {
        "statusCode": status_code,
        "headers": cors_headers(),
        "body": json.dumps({"error": message}),
    }
