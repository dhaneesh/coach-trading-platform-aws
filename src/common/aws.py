import json
import os
import boto3

dynamodb = boto3.resource("dynamodb")
ssm = boto3.client("ssm")
secretsmanager = boto3.client("secretsmanager")

def table():
    return dynamodb.Table(os.environ["DYNAMODB_TABLE"])

def get_parameter(name: str) -> str:
    return ssm.get_parameter(Name=name)["Parameter"]["Value"]

def get_secret(secret_arn: str) -> dict:
    return json.loads(secretsmanager.get_secret_value(SecretId=secret_arn)["SecretString"])
