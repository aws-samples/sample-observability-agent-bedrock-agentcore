# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Metrics MCP Server for Amazon Managed Prometheus.

This MCP server provides tools for querying metrics from Amazon Managed
Service for Prometheus using PromQL.

Security:
- Uses AWS IAM for authentication via SigV4 (no API keys)
- All connections use TLS
- Input validation on all parameters
"""

import os
import requests
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from mcp.server import FastMCP

mcp = FastMCP("Metrics Observability Server")

# =============================================================================
# Configuration - Set via environment variables
# =============================================================================
AMP_WORKSPACE_ID = os.environ.get("AMP_WORKSPACE_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Security limits
MAX_QUERY_LENGTH = 500
CONNECTION_TIMEOUT = 30


def _validate_config() -> None:
    """Validate required configuration."""
    if not AMP_WORKSPACE_ID:
        raise ValueError("AMP_WORKSPACE_ID environment variable is required")


def _sanitize_query(query: str) -> str:
    """Sanitize PromQL query - limit length."""
    if not query:
        return ""
    return query[:MAX_QUERY_LENGTH]


def _amp_query(endpoint: str, params: dict) -> dict:
    """
    Execute authenticated request to Amazon Managed Prometheus.
    
    Uses AWS SigV4 authentication - no API keys required.
    """
    _validate_config()
    
    url = f"https://aps-workspaces.{AWS_REGION}.amazonaws.com/workspaces/{AMP_WORKSPACE_ID}/api/v1/{endpoint}"
    
    # Sign request with SigV4
    session = boto3.Session()
    credentials = session.get_credentials()
    request = AWSRequest(method="GET", url=url, params=params)
    SigV4Auth(credentials, "aps", AWS_REGION).add_auth(request)
    
    response = requests.get(
        url,
        params=params,
        headers=dict(request.headers),
        timeout=CONNECTION_TIMEOUT,
    )
    
    return response.json()


# =============================================================================
# Metrics Tools
# =============================================================================

@mcp.tool(description="Execute instant PromQL query for current metric values")
def query_instant(query: str, time: str = None) -> dict:
    """
    Execute instant PromQL query.
    
    Args:
        query: PromQL expression (e.g., "up", "rate(http_requests_total[5m])")
        time: Optional evaluation timestamp (RFC3339 or Unix timestamp)
    
    Returns:
        Prometheus query result with current metric values.
    """
    query = _sanitize_query(query)
    if not query:
        return {"error": "Query is required", "status": "error"}
    
    params = {"query": query}
    if time:
        # Sanitize time parameter - allow only digits, colons, hyphens, T, Z
        time = "".join(c for c in time if c.isalnum() or c in ":-TZ.")[:30]
        params["time"] = time
    
    return _amp_query("query", params)


@mcp.tool(description="Execute PromQL range query for time series data")
def query_range(
    query: str,
    start: str,
    end: str,
    step: str = "60s",
) -> dict:
    """
    Execute range PromQL query for time series data.
    
    Args:
        query: PromQL expression
        start: Start timestamp (RFC3339 or Unix timestamp)
        end: End timestamp (RFC3339 or Unix timestamp)
        step: Query resolution step (e.g., "15s", "1m", "5m")
    
    Returns:
        Prometheus query result with time series data.
    """
    query = _sanitize_query(query)
    if not query:
        return {"error": "Query is required", "status": "error"}
    
    # Validate step format
    valid_steps = ["15s", "30s", "60s", "1m", "5m", "15m", "30m", "1h"]
    if step not in valid_steps:
        step = "60s"
    
    # Sanitize timestamps
    start = "".join(c for c in start if c.isalnum() or c in ":-TZ.")[:30]
    end = "".join(c for c in end if c.isalnum() or c in ":-TZ.")[:30]
    
    params = {
        "query": query,
        "start": start,
        "end": end,
        "step": step,
    }
    
    return _amp_query("query_range", params)


@mcp.tool(description="Get available metric names matching a pattern")
def list_metrics(match: str = None) -> dict:
    """
    List available metric names.
    
    Args:
        match: Optional series selector to filter metrics
    
    Returns:
        List of metric names available in Prometheus.
    """
    params = {}
    if match:
        match = _sanitize_query(match)
        params["match[]"] = match
    
    return _amp_query("label/__name__/values", params)


@mcp.tool(description="Get metadata for a specific metric")
def get_metric_metadata(metric: str) -> dict:
    """
    Get metadata for a metric including type and help text.
    
    Args:
        metric: Metric name to get metadata for
    
    Returns:
        Metric metadata including type, help, and unit.
    """
    if not metric:
        return {"error": "Metric name is required", "status": "error"}
    
    # Sanitize metric name - allow only valid Prometheus metric characters
    metric = "".join(c for c in metric if c.isalnum() or c in "_:")[:100]
    
    return _amp_query("metadata", {"metric": metric})


if __name__ == "__main__":
    mcp.run(transport="sse")
