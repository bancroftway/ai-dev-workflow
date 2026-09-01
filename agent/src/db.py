"""SQL Server connection helper -- single place that knows how to reach the sessions DB, local
dev or Azure. Used by db_migrate.py and session_store.py so neither reinvents the auth-mode
branch.

Local dev: Windows Trusted_Connection (matches the already-provisioned localhost `Ai-Dev-Workflow`
DB, no secrets involved). Azure: AAD auth via a token fetched ourselves with DefaultAzureCredential
and handed to pyodbc via SQL_COPT_SS_ACCESS_TOKEN -- NOT driver-native ActiveDirectoryMsi, which
has a documented history of breaking against Container Apps' App-Service-style managed-identity
endpoint (IDENTITY_ENDPOINT/IDENTITY_HEADER), unlike classic VM IMDS.

Mode is selected by whether AZURE_SQL_SERVER is set.
"""

from __future__ import annotations

import os
import struct

import pyodbc

_DRIVER = "{ODBC Driver 18 for SQL Server}"
_SQL_COPT_SS_ACCESS_TOKEN = 1256


def _local_connection_string() -> str:
    server = os.environ.get("AIDW_SQL_LOCAL_SERVER", "localhost")
    database = os.environ.get("AIDW_SQL_LOCAL_DATABASE", "Ai-Dev-Workflow")
    return (
        f"DRIVER={_DRIVER};SERVER={server};DATABASE={database};"
        "Trusted_Connection=yes;TrustServerCertificate=yes;"
    )


def _azure_connection_string() -> str:
    server = os.environ["AZURE_SQL_SERVER"]
    database = os.environ["AZURE_SQL_DATABASE"]
    return f"DRIVER={_DRIVER};SERVER={server};DATABASE={database};Encrypt=yes;"


def _azure_access_token_struct() -> bytes:
    from azure.identity import DefaultAzureCredential

    token = DefaultAzureCredential().get_token("https://database.windows.net/.default").token
    token_bytes = token.encode("utf-16-le")
    return struct.pack("=i", len(token_bytes)) + token_bytes


def is_azure() -> bool:
    return bool(os.environ.get("AZURE_SQL_SERVER"))


def connect() -> pyodbc.Connection:
    """Sync connection -- migration runner and session_store's self-check use this directly;
    session_store's async pool wraps the same dsn/attrs via connection_kwargs() below."""
    if is_azure():
        return pyodbc.connect(
            _azure_connection_string(),
            attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: _azure_access_token_struct()},
        )
    return pyodbc.connect(_local_connection_string())


def connection_kwargs() -> dict:
    """dsn/attrs_before for aioodbc.create_pool(), mirroring connect() for the async driver."""
    if is_azure():
        return {
            "dsn": _azure_connection_string(),
            "attrs_before": {_SQL_COPT_SS_ACCESS_TOKEN: _azure_access_token_struct()},
        }
    return {"dsn": _local_connection_string()}
