"""
Lambda: get_file.py
Endpoint: GET /arquivo

Responsabilidades:
  1. Verificar se o usuário é o proprietário do arquivo
     (arquivo_id deve começar com documentos/users/{user_id}/)
  2. Verificar se o arquivo existe no S3
  3. Gerar Pre-Signed URL com validade de 1 hora
  4. Retornar APENAS a URL — nenhum objeto é público

Opcionalmente:
  - Retorna também os metadados do JSON (status, resumo Bedrock, resultado Macie)
"""
import json
import os
import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")

DOCS_BUCKET     = os.environ["DOCS_BUCKET"]
METADATA_BUCKET = os.environ["METADATA_BUCKET"]
URL_EXPIRATION  = int(os.environ.get("PRESIGNED_URL_EXPIRATION", "3600"))


def handler(event, context):
    try:
        # ── 1. Extrair parâmetros ─────────────────────────────────
        query      = event.get("queryStringParameters") or {}
        user_id    = query.get("user_id", "").strip()
        arquivo_id = query.get("arquivo_id", "").strip()

        if not user_id:
            return error_response(400, "Parâmetro 'user_id' é obrigatório.")
        if not arquivo_id:
            return error_response(400, "Parâmetro 'arquivo_id' é obrigatório.")

        # ── 2. SEGURANÇA: verificar ownership ────────────────────
        # O arquivo_id DEVE começar com documentos/users/{user_id}/
        # Se não começar, o usuário está tentando acessar arquivo alheio.
        prefixo_esperado = f"documentos/users/{user_id}/"
        if not arquivo_id.startswith(prefixo_esperado):
            print(f"[ALERTA] Acesso negado: user={user_id} tentou acessar {arquivo_id}")
            return error_response(403, "Acesso negado. Este arquivo não pertence a você.")

        # ── 3. Verificar existência do arquivo no S3 ─────────────
        try:
            s3.head_object(Bucket=DOCS_BUCKET, Key=arquivo_id)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return error_response(404, "Arquivo não encontrado.")
            raise

        # ── 4. Gerar Pre-Signed URL ───────────────────────────────
        # A URL é assinada com as credenciais da IAM Role da Lambda.
        # Ela é válida apenas para este objeto específico
        # e expira automaticamente após URL_EXPIRATION segundos.
        presigned_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": DOCS_BUCKET, "Key": arquivo_id},
            ExpiresIn=URL_EXPIRATION,
        )

        # ── 5. Ler metadados (opcional — enriquece a resposta) ────
        metadata = read_metadata(arquivo_id)

        print(f"[GET_FILE] Acesso: user={user_id} arquivo={arquivo_id}")

        return {
            "statusCode": 200,
            "headers": cors_headers(),
            "body": json.dumps({
                "arquivo_id":       arquivo_id,
                "url":              presigned_url,
                "expira_em_segundos": URL_EXPIRATION,
                "metadata":         metadata,
            }),
        }

    except Exception as e:
        print(f"[ERRO] get_file: {str(e)}")
        return error_response(500, "Erro interno ao recuperar o arquivo.")


def read_metadata(doc_key: str) -> dict | None:
    """
    Tenta ler o JSON de metadados correspondente ao documento.
    Retorna None se não encontrar (não bloqueia o fluxo principal).
    """
    try:
        meta_key = doc_key.replace("documentos/", "metadata/") + ".json"
        meta_obj = s3.get_object(Bucket=METADATA_BUCKET, Key=meta_key)
        return json.loads(meta_obj["Body"].read())
    except ClientError:
        return None


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
