"""
Black Box IA — Stack principal CDK.

Serviços provisionados:
  - 2 buckets S3 (documentos + metadados)
  - 3 funções Lambda (upload, processor, get_file)
  - API Gateway REST
  - IAM Roles com menor privilégio
  - CloudWatch Logs + Alarmes
  - SNS para alertas
  - Amazon Macie (habilitado via CfnSession)
"""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_s3 as s3,
    aws_lambda as _lambda,
    aws_lambda_event_sources as lambda_events,
    aws_apigateway as apigw,
    aws_iam as iam,
    aws_logs as logs,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    aws_macie as macie,
)
from constructs import Construct


class BlackBoxStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ──────────────────────────────────────────────────
        # 1. SNS — tópico de alertas (criado primeiro para
        #    ser referenciado pelos alarmes CloudWatch)
        # ──────────────────────────────────────────────────
        alert_topic = sns.Topic(
            self,
            "AlertTopic",
            topic_name="blackbox-alerts",
            display_name="Black Box IA — Alertas operacionais",
        )

        # Troque pelo e-mail real do administrador antes do deploy
        alert_topic.add_subscription(
            sns_subs.EmailSubscription("admin@blackbox-ia.com")
        )

        # ──────────────────────────────────────────────────
        # 2. S3 — bucket principal (documentos)
        #
        # Estrutura:
        #   documentos/users/{userId}/arquivo.pdf
        #
        # Configurações obrigatórias:
        #   - Block Public Access
        #   - Criptografia SSE-S3
        #   - Versionamento
        #   - Lifecycle: 365d → Glacier Instant Retrieval
        # ──────────────────────────────────────────────────
        docs_bucket = s3.Bucket(
            self,
            "DocumentosBucket",
            bucket_name=f"blackbox-documentos-{self.account}-{self.region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
            enforce_ssl=True,  # somente HTTPS
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ArquivarDocumentosAntigos",
                    enabled=True,
                    # Após 365 dias → Glacier Instant Retrieval
                    # Justificativa: custo ~68% menor que Standard,
                    # mantendo acesso imediato para treino de IA futura.
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                            transition_after=Duration.days(365),
                        )
                    ],
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ──────────────────────────────────────────────────
        # 3. S3 — bucket de metadados (JSON por documento)
        #
        # Estrutura:
        #   metadata/users/{userId}/documento.json
        #
        # Armazena: userId, nome original, chave S3,
        # data de upload, status, resultado Macie, resumo Bedrock
        # ──────────────────────────────────────────────────
        metadata_bucket = s3.Bucket(
            self,
            "MetadataBucket",
            bucket_name=f"blackbox-metadata-{self.account}-{self.region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ──────────────────────────────────────────────────
        # 4. CloudWatch Log Group compartilhado
        # ──────────────────────────────────────────────────
        log_group = logs.LogGroup(
            self,
            "BlackBoxLogGroup",
            log_group_name="/blackbox-ia/lambdas",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ──────────────────────────────────────────────────
        # 5. IAM — Role base para todas as Lambdas
        #    (menor privilégio: só o que cada função precisa
        #     é concedido individualmente abaixo)
        # ──────────────────────────────────────────────────
        base_lambda_role = iam.Role(
            self,
            "LambdaBaseRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        # ──────────────────────────────────────────────────
        # 6. Lambda — upload.py
        #    Disparada por: POST /upload (API Gateway)
        #    Responsabilidades:
        #      - Validar autenticação (user_id)
        #      - Salvar arquivo no bucket de documentos
        #      - Criar JSON de metadados no bucket de metadados
        # ──────────────────────────────────────────────────
        upload_role = iam.Role(
            self,
            "UploadLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        # Permissão mínima: gravar nos dois buckets
        docs_bucket.grant_put(upload_role)
        metadata_bucket.grant_read_write(upload_role)

        fn_upload = _lambda.Function(
            self,
            "UploadFunction",
            function_name="blackbox-upload",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="upload.handler",
            code=_lambda.Code.from_asset("lambda"),
            role=upload_role,
            environment={
                "DOCS_BUCKET": docs_bucket.bucket_name,
                "METADATA_BUCKET": metadata_bucket.bucket_name,
            },
            timeout=Duration.seconds(30),
            memory_size=256,
            log_group=log_group,
        )

        # ──────────────────────────────────────────────────
        # 7. Lambda — processor.py
        #    Disparada por: evento ObjectCreated no bucket
        #    de documentos (apenas pasta documentos/users/)
        #    Responsabilidades:
        #      - Chamar Amazon Macie (scan do objeto)
        #      - Chamar Amazon Bedrock (resumo do documento)
        #      - Atualizar JSON de metadados com os resultados
        # ──────────────────────────────────────────────────
        processor_role = iam.Role(
            self,
            "ProcessorLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        # Permissões mínimas do processor
        docs_bucket.grant_read(processor_role)
        metadata_bucket.grant_read_write(processor_role)

        # Permissão para chamar Macie (criar classification job)
        processor_role.add_to_policy(
            iam.PolicyStatement(
                sid="MaciePermissions",
                effect=iam.Effect.ALLOW,
                actions=[
                    "macie2:CreateClassificationJob",
                    "macie2:GetClassificationJob",
                    "macie2:ListClassificationJobs",
                ],
                resources=["*"],
            )
        )

        # Permissão para chamar Bedrock (Amazon Titan Text)
        processor_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockPermissions",
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel"],
                # Titan Text Express — gratuito no Free Tier
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-text-express-v1"
                ],
            )
        )

        fn_processor = _lambda.Function(
            self,
            "ProcessorFunction",
            function_name="blackbox-processor",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="processor.handler",
            code=_lambda.Code.from_asset("lambda"),
            role=processor_role,
            environment={
                "DOCS_BUCKET": docs_bucket.bucket_name,
                "METADATA_BUCKET": metadata_bucket.bucket_name,
                "BEDROCK_MODEL_ID": "amazon.titan-text-express-v1",
                "ALERT_TOPIC_ARN": alert_topic.topic_arn,
            },
            timeout=Duration.seconds(120),  # Bedrock pode levar mais tempo
            memory_size=512,
            log_group=log_group,
        )

        # Permissão para publicar alertas no SNS
        alert_topic.grant_publish(processor_role)

        # Gatilho S3 → processor (apenas pasta documentos/users/)
        fn_processor.add_event_source(
            lambda_events.S3EventSource(
                docs_bucket,
                events=[s3.EventType.OBJECT_CREATED],
                filters=[s3.NotificationKeyFilter(prefix="documentos/users/")],
            )
        )

        # ──────────────────────────────────────────────────
        # 8. Lambda — get_file.py
        #    Disparada por: GET /arquivo (API Gateway)
        #    Responsabilidades:
        #      - Verificar se o usuário é o proprietário
        #      - Gerar Pre-Signed URL (1 hora)
        #      - Retornar apenas a URL (arquivo nunca é público)
        # ──────────────────────────────────────────────────
        get_file_role = iam.Role(
            self,
            "GetFileLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        # Permissão mínima: ler do bucket de documentos e metadados
        docs_bucket.grant_read(get_file_role)
        metadata_bucket.grant_read(get_file_role)

        fn_get_file = _lambda.Function(
            self,
            "GetFileFunction",
            function_name="blackbox-get-file",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="get_file.handler",
            code=_lambda.Code.from_asset("lambda"),
            role=get_file_role,
            environment={
                "DOCS_BUCKET": docs_bucket.bucket_name,
                "METADATA_BUCKET": metadata_bucket.bucket_name,
                "PRESIGNED_URL_EXPIRATION": "3600",
            },
            timeout=Duration.seconds(10),
            memory_size=128,
            log_group=log_group,
        )

        # ──────────────────────────────────────────────────
        # 9. API Gateway
        # ──────────────────────────────────────────────────
        api = apigw.RestApi(
            self,
            "BlackBoxApi",
            rest_api_name="blackbox-ia-api",
            description="Black Box IA — API de gestão de documentos",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Content-Type", "Authorization"],
            ),
        )

        # POST /upload
        upload_resource = api.root.add_resource("upload")
        upload_resource.add_method(
            "POST",
            apigw.LambdaIntegration(fn_upload),
            method_responses=[apigw.MethodResponse(status_code="200")],
        )

        # GET /arquivo
        arquivo_resource = api.root.add_resource("arquivo")
        arquivo_resource.add_method(
            "GET",
            apigw.LambdaIntegration(fn_get_file),
            method_responses=[apigw.MethodResponse(status_code="200")],
        )

        # ──────────────────────────────────────────────────
        # 10. Amazon Macie — habilitar via CDK
        #
        # LIMITAÇÃO: O CDK consegue habilitar o Macie na conta
        # (CfnSession), mas não cria Classification Jobs
        # automaticamente em contas novas — isso exige que o
        # Macie já esteja ativo por pelo menos alguns minutos.
        #
        # O que o CDK faz: habilita o Macie.
        # O que precisa ser feito manualmente:
        #   AWS Console → Macie → Jobs → Create Job
        #   selecionar o bucket de documentos
        #   frequência: sob demanda ou periódica
        # ──────────────────────────────────────────────────
        macie.CfnSession(
            self,
            "MacieSession",
            finding_publishing_frequency="FIFTEEN_MINUTES",
            status="ENABLED",
        )

        # ──────────────────────────────────────────────────
        # 11. CloudWatch — alarmes operacionais
        # ──────────────────────────────────────────────────

        # Alarme: erros na Lambda de upload
        upload_errors = cw.Alarm(
            self,
            "UploadErrorAlarm",
            alarm_name="blackbox-upload-errors",
            alarm_description="Erros na Lambda de upload",
            metric=fn_upload.metric_errors(period=Duration.minutes(5)),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        upload_errors.add_alarm_action(
            cw_actions.SnsAction(alert_topic)
        )

        # Alarme: erros na Lambda de processamento (Macie + Bedrock)
        processor_errors = cw.Alarm(
            self,
            "ProcessorErrorAlarm",
            alarm_name="blackbox-processor-errors",
            alarm_description="Falha no processamento Macie/Bedrock",
            metric=fn_processor.metric_errors(period=Duration.minutes(5)),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        processor_errors.add_alarm_action(
            cw_actions.SnsAction(alert_topic)
        )

        # Alarme: erros na Lambda de consulta
        get_file_errors = cw.Alarm(
            self,
            "GetFileErrorAlarm",
            alarm_name="blackbox-get-file-errors",
            alarm_description="Erros na Lambda de consulta/Pre-Signed URL",
            metric=fn_get_file.metric_errors(period=Duration.minutes(5)),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        get_file_errors.add_alarm_action(
            cw_actions.SnsAction(alert_topic)
        )

        # ──────────────────────────────────────────────────
        # 12. Outputs — valores impressos após o deploy
        # ──────────────────────────────────────────────────
        CfnOutput(self, "ApiUrl",
            value=api.url,
            description="URL base da API")

        CfnOutput(self, "DocsBucketName",
            value=docs_bucket.bucket_name,
            description="Bucket de documentos")

        CfnOutput(self, "MetadataBucketName",
            value=metadata_bucket.bucket_name,
            description="Bucket de metadados JSON")

        CfnOutput(self, "AlertTopicArn",
            value=alert_topic.topic_arn,
            description="ARN do tópico SNS de alertas")
