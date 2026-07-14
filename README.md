# Black Box IA — Gestão de Documentos para IA

Plataforma serverless na AWS para armazenamento seguro de documentos,
com análise automática via Amazon Macie e Amazon Bedrock.

## Arquitetura

```
Cliente
  → API Gateway
  → Lambda upload.py       → S3 (docs) + S3 (metadata JSON)
                                ↓ evento ObjectCreated
  → Lambda processor.py    → Macie (scan) + Bedrock (resumo)
                                ↓ atualiza metadata JSON
  → Lambda get_file.py     → verifica ownership → Pre-Signed URL (1h)
```

## Serviços utilizados

| Serviço | Função |
|---|---|
| Amazon API Gateway | Entrada HTTP — POST /upload e GET /arquivo |
| AWS Lambda (x3) | upload, processor, get_file |
| Amazon S3 (docs) | Armazena documentos — Block Public Access + Lifecycle |
| Amazon S3 (metadata) | Armazena metadados JSON por documento |
| AWS IAM | Menor privilégio — role separada por Lambda |
| Amazon Macie | Detecta dados sensíveis (CPF, RG, cartão) nos documentos |
| Amazon Bedrock | Gera resumo, tópicos e palavras-chave (Titan Text Express) |
| Amazon CloudWatch | Logs + alarmes de erro por Lambda |
| Amazon SNS | Alertas: dado sensível detectado + erros operacionais |
| AWS CDK (Python) | Infraestrutura como Código — deploy e destroy em minutos |

## Estrutura do projeto

```
blackbox/
├── app.py                   # Ponto de entrada do CDK
├── cdk.json                 # Configuração do CDK
├── requirements.txt         # Dependências
├── README.md
│
├── infrastructure/
│   ├── __init__.py
│   └── stack.py             # Todos os recursos AWS definidos aqui
│
└── lambda/
    ├── upload.py            # POST /upload
    ├── processor.py         # Trigger S3 → Macie + Bedrock
    └── get_file.py          # GET /arquivo → Pre-Signed URL
```

## Estrutura dos buckets S3

### Bucket de documentos
```
documentos/
  users/
    {userId}/
      20260714_120000_contrato.pdf
      20260714_130000_rg.pdf
```

### Bucket de metadados
```
metadata/
  users/
    {userId}/
      20260714_120000_contrato.pdf.json
      20260714_130000_rg.pdf.json
```

## Exemplo de JSON de metadados

```json
{
  "doc_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "user_id": "usuario123",
  "filename": "contrato.pdf",
  "s3_key": "documentos/users/usuario123/20260714_120000_contrato.pdf",
  "metadata_key": "metadata/users/usuario123/20260714_120000_contrato.pdf.json",
  "bucket": "blackbox-documentos-123456789-us-east-1",
  "upload_timestamp": "2026-07-14T12:00:00+00:00",
  "content_type": "application/pdf",
  "status": "processed",
  "processed_at": "2026-07-14T12:01:30+00:00",

  "macie_result": {
    "job_id": "abc123def456",
    "status": "complete",
    "sensitive_data_found": false,
    "finding_types": [],
    "note": "Job concluído. Consulte AWS Console → Macie → Findings."
  },
  "macie_scan_at": "2026-07-14T12:01:00+00:00",

  "bedrock_summary": {
    "status": "success",
    "model": "amazon.titan-text-express-v1",
    "summary": "O documento trata de um contrato de prestação de serviços entre as partes A e B, com vigência de 12 meses.",
    "topics": ["Contrato", "Prestação de serviços", "Vigência"],
    "keywords": ["contrato", "serviços", "prazo", "rescisão", "pagamento"]
  },
  "bedrock_summary_at": "2026-07-14T12:01:25+00:00"
}
```

## Pré-requisitos

- Python 3.10+
- Node.js 18+ (necessário para o CDK)
- AWS CLI configurado (`aws configure`)
- CDK instalado: `npm install -g aws-cdk`

## Como rodar

### 1. Ambiente virtual e dependências

```bash
python3 -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows

pip install -r requirements.txt
```

### 2. Habilitar o Amazon Bedrock (manual — necessário)

O Bedrock exige habilitação manual do modelo na conta AWS:

1. AWS Console → Amazon Bedrock → Model access
2. Clicar em **Manage model access**
3. Habilitar **Amazon Titan Text Express**
4. Aguardar aprovação (geralmente imediata)

### 3. Bootstrap e deploy

```bash
cdk bootstrap
cdk synth    # verificar template sem criar recursos
cdk deploy
```

Outputs após o deploy:
```
BlackBoxStack.ApiUrl              = https://xxx.execute-api.us-east-1.amazonaws.com/prod/
BlackBoxStack.DocsBucketName      = blackbox-documentos-123456789-us-east-1
BlackBoxStack.MetadataBucketName  = blackbox-metadata-123456789-us-east-1
BlackBoxStack.AlertTopicArn       = arn:aws:sns:us-east-1:...
```

### 4. Configurar o Macie (manual — após o deploy)

O CDK habilita o Macie automaticamente, mas criar Classification Jobs
em contas novas exige configuração manual:

1. AWS Console → Amazon Macie → Jobs → **Create job**
2. Selecionar: bucket `blackbox-documentos-*`
3. Escopo: pasta `documentos/users/`
4. Frequência: **On demand** (para testes) ou **Daily**
5. Confirmar e criar

A Lambda processor.py cria jobs automaticamente via API para cada upload.

### 5. Confirmar e-mail do SNS

Após o deploy, a AWS enviará um e-mail para `admin@blackbox-ia.com`
com um link de confirmação. Confirme antes de testar os alarmes.
Edite o e-mail em `infrastructure/stack.py` antes do deploy.

## Testando

### Upload de arquivo

```bash
curl -X POST "https://xxx.execute-api.us-east-1.amazonaws.com/prod/upload?user_id=usuario1&filename=teste.pdf" \
  -H "Content-Type: application/pdf" \
  --data-binary @tests/teste.pdf
```

Resposta esperada:
```json
{
  "message": "Arquivo enviado com sucesso.",
  "arquivo_id": "documentos/users/usuario1/20260714_120000_teste.pdf",
  "doc_id": "f47ac10b-...",
  "filename": "teste.pdf",
  "timestamp": "20260714_120000"
}
```

### Consultar arquivo e obter Pre-Signed URL

```bash
curl -X GET "https://xxx.execute-api.us-east-1.amazonaws.com/prod/arquivo?\
user_id=usuario1&arquivo_id=documentos/users/usuario1/20260714_120000_teste.pdf"
```

Resposta esperada:
```json
{
  "arquivo_id": "documentos/users/usuario1/20260714_120000_teste.pdf",
  "url": "https://blackbox-documentos-xxx.s3.amazonaws.com/...?X-Amz-Signature=...",
  "expira_em_segundos": 3600,
  "metadata": { ... }
}
```

### Teste de segurança (deve retornar 403)

```bash
curl -X GET "https://xxx.execute-api.us-east-1.amazonaws.com/prod/arquivo?\
user_id=usuario1&arquivo_id=documentos/users/outro_usuario/arquivo.pdf"
```

## Destruir recursos (evitar custos)

```bash
cdk destroy
```

> Os buckets S3 têm `RemovalPolicy.RETAIN` — os arquivos NÃO são deletados.
> Para deletar manualmente: AWS Console → S3 → esvaziar bucket → excluir.

## Limitações conhecidas e próximos passos

| Limitação atual | Solução futura |
|---|---|
| `user_id` via query param (sem auth) | Amazon Cognito — token JWT |
| PDFs binários sem extração de texto | Amazon Textract antes do Bedrock |
| Macie: job criado por arquivo (lento) | Job periódico + EventBridge para findings |
| Sem frontend | ActivePieces + Tally para fluxo completo |
