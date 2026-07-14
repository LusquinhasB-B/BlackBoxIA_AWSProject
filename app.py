#!/usr/bin/env python3
"""
Black Box IA — Ponto de entrada do CDK.
Instancia a stack principal e sintetiza o template CloudFormation.
"""
import aws_cdk as cdk
from infrastructure.stack import BlackBoxStack

app = cdk.App()

BlackBoxStack(
    app,
    "BlackBoxStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account"),
        region=app.node.try_get_context("region") or "us-east-1",
    ),
    description="Black Box IA — Plataforma serverless de gestão de documentos para IA",
)

app.synth()
