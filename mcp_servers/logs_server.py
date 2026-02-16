# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Logs MCP Server for Amazon OpenSearch Serverless.

Provides tools for searching and filtering application logs
stored in OpenSearch following OpenTelemetry conventions.
"""

import os
import boto3
from mcp.server import FastMCP
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

mcp = FastMCP("Logs MCP Server")

OPENSEARCH_HOST = os.environ.get("OPENSEARCH_HOST", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_RESULTS = 200
CONNECTION_TIMEOUT = 30

_client = None


def _sanitize(value: str, max_len: int = 100) -> str:
    if not value:
        return ""
    return "".join(c for c in value if c.isalnum() or c in "-_.")[:max_len]


def _sanitize_trace_id(tid: str) -> str:
    if not tid:
        return ""
    return "".join(c for c in tid if c in "0123456789abcdefABCDEF")[:32]


def get_client() -> OpenSearch:
    global _client
    if _client is None:
        if not OPENSEARCH_HOST:
            raise ValueError("OPENSEARCH_HOST environment variable is required")
        credentials = boto3.Session().get_credentials()
        _client = OpenSearch(
            hosts=[{"host": OPENSEARCH_HOST, "port": 443}],
            http_auth=AWS4Auth(credentials.access_key, credentials.secret_key, AWS_REGION, "aoss", session_token=credentials.token),
            use_ssl=True, verify_certs=True,
            connection_class=RequestsHttpConnection, timeout=CONNECTION_TIMEOUT,
        )
    return _client


@mcp.tool(description="Search OpenTelemetry logs with filters for service, severity, and time range")
def search_otel_logs(service: str = None, severity: str = None, start_time: str = "now-1h", end_time: str = "now", size: int = 50) -> dict:
    """Search logs in OpenSearch."""
    size = min(max(1, size), MAX_RESULTS)
    valid_ranges = ["now-5m", "now-15m", "now-30m", "now-1h", "now-2h", "now-6h", "now-12h", "now-24h"]
    if start_time not in valid_ranges:
        start_time = "now-1h"

    must = [{"range": {"@timestamp": {"gte": start_time, "lte": end_time}}}]
    if service:
        must.append({"term": {"resource.service.name": _sanitize(service, 50)}})
    if severity and severity.upper() in ["INFO", "WARN", "ERROR", "DEBUG"]:
        must.append({"term": {"severityText": severity.upper()}})

    return get_client().search(index="otel-logs-*", body={"query": {"bool": {"must": must}}, "sort": [{"@timestamp": "desc"}], "size": size})


@mcp.tool(description="Get all logs correlated with a specific trace ID")
def get_logs_by_trace_id(trace_id: str, size: int = 100) -> dict:
    """Retrieve logs for a specific trace."""
    trace_id = _sanitize_trace_id(trace_id)
    if not trace_id:
        return {"error": "Invalid trace_id format"}
    size = min(max(1, size), MAX_RESULTS)
    return get_client().search(index="otel-logs-*", body={"query": {"term": {"traceId": trace_id}}, "sort": [{"@timestamp": "asc"}], "size": size})


if __name__ == "__main__":
    mcp.run(transport="sse")
