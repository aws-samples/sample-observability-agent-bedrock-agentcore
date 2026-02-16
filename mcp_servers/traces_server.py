# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Traces MCP Server for Amazon OpenSearch Serverless.

Provides tools for searching distributed traces, spans, service maps,
and RED metrics stored in OpenSearch following OpenTelemetry conventions.
"""

import os
import boto3
from mcp.server import FastMCP
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

mcp = FastMCP("Traces MCP Server")

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


@mcp.tool(description="Search OpenTelemetry spans with filters for service, operation, and time range")
def get_otel_spans(service_name: str = None, trace_id: str = None, operation_name: str = None, start_time: str = "now-1h", end_time: str = "now", size: int = 50) -> dict:
    """Search spans in OpenSearch."""
    size = min(max(1, size), MAX_RESULTS)
    valid_ranges = ["now-5m", "now-15m", "now-30m", "now-1h", "now-2h", "now-6h", "now-12h", "now-24h"]
    if start_time not in valid_ranges:
        start_time = "now-1h"

    must = [{"range": {"startTime": {"gte": start_time, "lte": end_time}}}]
    if service_name:
        must.append({"term": {"serviceName": _sanitize(service_name, 50)}})
    if trace_id:
        tid = _sanitize_trace_id(trace_id)
        if tid:
            must.append({"term": {"traceId": tid}})
    if operation_name:
        must.append({"term": {"name": _sanitize(operation_name, 100)}})

    return get_client().search(index="otel-v1-apm-span-*", body={"query": {"bool": {"must": must}}, "sort": [{"startTime": "desc"}], "size": size})


@mcp.tool(description="Get all spans for a specific trace ID to analyze request flow")
def get_spans_by_trace_id(trace_id: str, size: int = 200) -> dict:
    """Retrieve all spans for a trace."""
    trace_id = _sanitize_trace_id(trace_id)
    if not trace_id:
        return {"error": "Invalid trace_id format"}
    size = min(max(1, size), MAX_RESULTS)
    return get_client().search(index="otel-v1-apm-span-*", body={"query": {"term": {"traceId": trace_id}}, "sort": [{"startTime": "asc"}], "size": size})


@mcp.tool(description="Get service map showing dependencies between services")
def get_otel_service_map(service_name: str = None, start_time: str = "now-1h", end_time: str = "now") -> dict:
    """Get service dependency map from traces."""
    valid_ranges = ["now-5m", "now-15m", "now-30m", "now-1h", "now-2h", "now-6h"]
    if start_time not in valid_ranges:
        start_time = "now-1h"

    must = [{"range": {"startTime": {"gte": start_time, "lte": end_time}}}]
    if service_name:
        must.append({"term": {"serviceName": _sanitize(service_name, 50)}})

    body = {"query": {"bool": {"must": must}}, "aggs": {"services": {"terms": {"field": "serviceName", "size": 50}, "aggs": {"destinations": {"terms": {"field": "destination.service.resource", "size": 50}}}}}, "size": 0}
    return get_client().search(index="otel-v1-apm-span-*", body=body)


@mcp.tool(description="Get Rate, Error, Duration (RED) metrics for services")
def get_otel_red_metrics(start_time: str = "now-1h", end_time: str = "now") -> dict:
    """Calculate RED metrics from trace data."""
    valid_ranges = ["now-5m", "now-15m", "now-30m", "now-1h", "now-2h", "now-6h", "now-12h", "now-24h"]
    if start_time not in valid_ranges:
        start_time = "now-1h"

    body = {"query": {"range": {"startTime": {"gte": start_time, "lte": end_time}}}, "aggs": {"services": {"terms": {"field": "serviceName", "size": 50}, "aggs": {"request_count": {"value_count": {"field": "traceId"}}, "error_count": {"filter": {"term": {"status.code": 2}}}, "avg_duration": {"avg": {"field": "durationInNanos"}}, "p99_duration": {"percentiles": {"field": "durationInNanos", "percents": [99]}}}}}, "size": 0}
    return get_client().search(index="otel-v1-apm-span-*", body=body)


if __name__ == "__main__":
    mcp.run(transport="sse")
