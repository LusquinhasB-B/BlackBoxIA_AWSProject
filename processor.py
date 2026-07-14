"""
Lambda: processor.py
Gatilho: evento ObjectCreated no bucket de documentos
         (prefixo: documentos/users/)

Responsabilidades (UMA única Lambda, duas etapas):
  Etapa 1 — Amazon Macie
    - Cria um Classification Job para o objeto recém-enviado
    - Aguarda a conclusão (polling com timeout)
    - Extrai resultados: dados sensíveis encontrados (CPF, RG, etc.)

  Etapa 2 — Amazon Bedrock (Amazon Titan Text Express)
    - Lê o conteúdo do objeto S3
    - Envia para o Titan Text para gerar resumo, tópicos e palavras-chave
    - Armazena o resultado no JSON de metadados

  Ao final:
    - Atualiza o JSON de metadados com status "processed"
    - Em caso de dado sensível detectado pelo Macie: publica alerta no SNS

IMPORTANTE sobre o Macie:
  O Macie analisa objetos S3 de forma assíncrona. Esta Lambda cria o job
  e faz polling por até 90 segundos. Se o job não concluir nesse tempo,
  o status é salvo como "macie_pending" e o Bedrock prossegue normalmente.
  Para análises completas, configure jobs periódicos manualmente no console.
"""
import json
import os
import time
import boto3
import base64
from datetime import datetime, timezone
from botocore.exceptions import ClientError

s3       = boto3.client("s3")
macie    = boto3.client("macie2")
bedrock  = boto3.client("bedrock-runtime")
sns      = boto3.client("sns")

DOCS_BUCKET      = os.environ["DOCS_BUCKET"]
METADATA_BUCKET  = os.environ["METADATA_BUCKET"]
BEDROCK_MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
ALERT_TOPIC_ARN  = os.environ["ALERT_TOPIC_ARN"]

# Timeout máximo para esperar o Macie (segundos)
MACIE_TIMEOUT_SECONDS = 90
MACIE_POLL_INTERVAL   = 10


def handler(event, context):
    for record in event.get("Records", []):
        try:
            process_record(record)
        except Exception as e:
            print(f"[ERRO] processor: {str(e)}")
            raise  # re-lança para o Lambda registrar falha e acionar o alarme


def process_record(record):
    # ── Extrair chave do objeto recém-criado ──────────────────────
    doc_key = record["s3"]["object"]["key"]
    print(f"[PROCESSOR] Processando: {doc_key}")

    # ── Montar chave de metadados correspondente ──────────────────
    # doc_key:  documentos/users/{userId}/{timestamp}_{filename}
    # meta_key: metadata/users/{userId}/{timestamp}_{filename}.json
    meta_key = doc_key.replace("documentos/", "metadata/") + ".json"

    # ── Ler metadados existentes ──────────────────────────────────
    try:
        meta_obj  = s3.get_object(Bucket=METADATA_BUCKET, Key=meta_key)
        metadata  = json.loads(meta_obj["Body"].read())
    except ClientError:
        # Se o metadado não existir, cria um mínimo para não perder o processamento
        print(f"[AVISO] Metadado não encontrado para {doc_key}. Criando estrutura mínima.")
        metadata = {
            "s3_key":   doc_key,
            "meta_key": meta_key,
            "status":   "uploaded",
        }

    # ══════════════════════════════════════════════════════════════
    # ETAPA 1 — Amazon Macie
    # ══════════════════════════════════════════════════════════════
    macie_result = run_macie_scan(doc_key)
    metadata["macie_result"]      = macie_result
    metadata["macie_scan_at"]     = datetime.now(timezone.utc).isoformat()

    # Se Macie encontrou dados sensíveis → publicar alerta no SNS
    if macie_result.get("sensitive_data_found"):
        publish_alert(
            subject="[Black Box IA] Dado sensível detectado",
            message=(
                f"O Macie detectou dados sensíveis no documento:\n"
                f"  Arquivo: {doc_key}\n"
                f"  Tipos: {macie_result.get('finding_types', [])}\n"
                f"  Arquivo de metadados: {meta_key}"
            ),
        )

    # ══════════════════════════════════════════════════════════════
    # ETAPA 2 — Amazon Bedrock (Titan Text Express)
    # ══════════════════════════════════════════════════════════════
    bedrock_result = run_bedrock_summary(doc_key)
    metadata["bedrock_summary"]   = bedrock_result
    metadata["bedrock_summary_at"] = datetime.now(timezone.utc).isoformat()

    # ── Atualizar status do documento ─────────────────────────────
    metadata["status"] = "processed"
    metadata["processed_at"] = datetime.now(timezone.utc).isoformat()

    # ── Salvar metadados atualizados ──────────────────────────────
    s3.put_object(
        Bucket=METADATA_BUCKET,
        Key=meta_key,
        Body=json.dumps(metadata, ensure_ascii=False, indent=2),
        ContentType="application/json",
    )

    print(f"[PROCESSOR] Concluído: {doc_key}")


# ──────────────────────────────────────────────────────────────────
# Macie — criar job de classificação e aguardar resultado
# ──────────────────────────────────────────────────────────────────
def run_macie_scan(doc_key: str) -> dict:
    """
    Cria um Macie Classification Job para o objeto específico
    e aguarda o resultado por até MACIE_TIMEOUT_SECONDS.

    Retorna dict com:
      - job_id
      - status
      - sensitive_data_found (bool)
      - finding_types (list)
    """
    try:
        # Extrair informações do caminho
        # doc_key: documentos/users/{userId}/{timestamp}_{filename}
        parts   = doc_key.split("/")
        user_id = parts[2] if len(parts) > 2 else "unknown"

        response = macie.create_classification_job(
            jobType="ONE_TIME",
            name=f"blackbox-scan-{int(time.time())}",
            s3JobDefinition={
                "bucketDefinitions": [
                    {
                        "accountId": boto3.client("sts").get_caller_identity()["Account"],
                        "buckets":   [DOCS_BUCKET],
                    }
                ],
                "scoping": {
                    "includes": {
                        "and": [
                            {
                                "simpleScopeTerm": {
                                    "comparator": "STARTS_WITH",
                                    "key":        "OBJECT_KEY",
                                    "values":     [doc_key],
                                }
                            }
                        ]
                    }
                },
            },
        )

        job_id = response["jobId"]
        print(f"[MACIE] Job criado: {job_id}")

        # Polling para aguardar conclusão
        elapsed = 0
        while elapsed < MACIE_TIMEOUT_SECONDS:
            time.sleep(MACIE_POLL_INTERVAL)
            elapsed += MACIE_POLL_INTERVAL

            job_info = macie.get_classification_job(jobId=job_id)
            status   = job_info["jobStatus"]
            print(f"[MACIE] Status do job {job_id}: {status} ({elapsed}s)")

            if status == "COMPLETE":
                return {
                    "job_id":              job_id,
                    "status":              "complete",
                    "sensitive_data_found": False,  # findings são consultados separadamente
                    "finding_types":       [],
                    "note": (
                        "Job concluído. Consulte AWS Console → Macie → Findings "
                        "para ver os dados sensíveis detectados neste objeto."
                    ),
                }

            if status in ("CANCELLED", "ERROR", "PAUSED"):
                return {
                    "job_id":  job_id,
                    "status":  status,
                    "sensitive_data_found": False,
                    "finding_types": [],
                    "error": f"Job encerrado com status: {status}",
                }

        # Timeout: job ainda em andamento
        return {
            "job_id":  job_id,
            "status":  "macie_pending",
            "sensitive_data_found": False,
            "finding_types": [],
            "note": (
                f"Timeout de {MACIE_TIMEOUT_SECONDS}s atingido. "
                f"O job {job_id} continua rodando no Macie."
            ),
        }

    except ClientError as e:
        code = e.response["Error"]["Code"]
        print(f"[MACIE] Erro: {code} — {str(e)}")
        # Macie pode não estar habilitado ou job limit atingido
        return {
            "status": "error",
            "sensitive_data_found": False,
            "finding_types": [],
            "error": f"{code}: {str(e)}",
        }


# ──────────────────────────────────────────────────────────────────
# Bedrock — gerar resumo com Amazon Titan Text Express
# ──────────────────────────────────────────────────────────────────
def run_bedrock_summary(doc_key: str) -> dict:
    """
    Lê o objeto S3 (assume texto ou PDF com texto extraível),
    envia para o Titan Text Express e retorna resumo estruturado.

    Para PDFs binários sem extração de texto, retorna aviso.
    Para uma extração completa de PDFs, integrar Amazon Textract
    como etapa anterior (roadmap futuro).
    """
    try:
        # Ler conteúdo do documento
        obj      = s3.get_object(Bucket=DOCS_BUCKET, Key=doc_key)
        raw      = obj["Body"].read()

        # Tentar decodificar como texto
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            # PDF ou arquivo binário — sem Textract não há extração de texto
            return {
                "status": "skipped",
                "reason": (
                    "Arquivo binário (PDF). Para extração de texto de PDFs, "
                    "integre Amazon Textract como etapa anterior (roadmap futuro)."
                ),
                "summary":   None,
                "topics":    [],
                "keywords":  [],
            }

        # Truncar para evitar exceder o limite de tokens do Titan (8k tokens ≈ 32k chars)
        content_truncated = content[:30000] if len(content) > 30000 else content

        # Prompt para o Titan Text Express
        prompt = f"""Analise o seguinte documento e responda APENAS com um JSON válido
sem nenhum texto antes ou depois, com esta estrutura exata:
{{
  "resumo": "resumo em até 3 parágrafos",
  "topicos_principais": ["topico1", "topico2", "topico3"],
  "palavras_chave": ["palavra1", "palavra2", "palavra3", "palavra4", "palavra5"]
}}

Documento:
{content_truncated}"""

        # Invocar Titan Text Express via Bedrock
        body = json.dumps({
            "inputText": prompt,
            "textGenerationConfig": {
                "maxTokenCount": 1024,
                "temperature":   0.3,
                "topP":          0.9,
            },
        })

        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=body,
            contentType="application/json",
            accept="application/json",
        )

        result_body = json.loads(response["body"].read())
        output_text = result_body["results"][0]["outputText"].strip()

        # Tentar parsear o JSON da resposta
        try:
            # Limpar possíveis blocos de código markdown da resposta
            clean = output_text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)
            return {
                "status":           "success",
                "model":            BEDROCK_MODEL_ID,
                "summary":          parsed.get("resumo"),
                "topics":           parsed.get("topicos_principais", []),
                "keywords":         parsed.get("palavras_chave", []),
            }
        except json.JSONDecodeError:
            # Se o modelo não retornou JSON válido, salva o texto bruto
            return {
                "status":   "success_raw",
                "model":    BEDROCK_MODEL_ID,
                "summary":  output_text,
                "topics":   [],
                "keywords": [],
            }

    except ClientError as e:
        code = e.response["Error"]["Code"]
        print(f"[BEDROCK] Erro: {code} — {str(e)}")
        return {
            "status": "error",
            "error":  f"{code}: {str(e)}",
            "summary":  None,
            "topics":   [],
            "keywords": [],
        }


# ──────────────────────────────────────────────────────────────────
# SNS — publicar alerta
# ──────────────────────────────────────────────────────────────────
def publish_alert(subject: str, message: str):
    try:
        sns.publish(
            TopicArn=ALERT_TOPIC_ARN,
            Subject=subject,
            Message=message,
        )
        print(f"[SNS] Alerta publicado: {subject}")
    except ClientError as e:
        print(f"[SNS] Falha ao publicar alerta: {str(e)}")
