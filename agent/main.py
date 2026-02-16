# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Observability Agent for Amazon Bedrock AgentCore Runtime.

This agent helps SREs investigate incidents by querying logs, traces, and metrics
from OpenSearch Serverless and Amazon Managed Prometheus.

Security considerations:
- Uses AWS IAM for authentication (no hardcoded credentials)
- All connections use TLS (verify_certs=True)
- Input validation on tool parameters
- Timeouts configured to prevent hanging connections
"""

import os
import logging
import requests
from typing import Optional

from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

# =============================================================================
# Configuration - Set via environment variables (no hardcoded values in production)
# For this sample, defaults point to the POC resources created by the Quick Start guide.
# In production, always set these via environment variables.
# =============================================================================
OPENSEARCH_HOST = os.environ.get(
    "OPENSEARCH_HOST",
    "wlq7t3ulv8shjzc0kw7a.us-east-1.aoss.amazonaws.com"  # POC default - replace in production
)
AMP_WORKSPACE_ID = os.environ.get(
    "AMP_WORKSPACE_ID",
    "ws-3c981eb9-e9f4-453c-b3f0-51dbe62b4866"  # POC default - replace in production
)
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Connection settings
CONNECTION_TIMEOUT = 30  # seconds
MAX_RESULTS = 100  # Maximum results to return from queries

# Cached OpenSearch client (singleton pattern)
_opensearch_client: Optional[OpenSearch] = None


def _validate_config() -> None:
    """Validate required configuration is present."""
    if not OPENSEARCH_HOST:
        raise ValueError("OPENSEARCH_HOST environment variable is required")


def get_opensearch_client() -> OpenSearch:
    """
    Create OpenSearch client with AWS SigV4 authentication.
    
    Uses IAM credentials from the execution environment (no hardcoded secrets).
    Connection is cached for reuse across invocations.
    
    Returns:
        OpenSearch: Configured client instance
        
    Raises:
        ValueError: If OPENSEARCH_HOST is not configured
    """
    global _opensearch_client
    
    if _opensearch_client is None:
        _validate_config()
        
        # Get credentials from IAM role (AgentCore Runtime provides these)
        session = boto3.Session()
        credentials = session.get_credentials()
        
        # Create SigV4 auth for OpenSearch Serverless
        awsauth = AWS4Auth(
            credentials.access_key,
            credentials.secret_key,
            AWS_REGION,
            "aoss",  # Service name for OpenSearch Serverless
            session_token=credentials.token,
        )
        
        _opensearch_client = OpenSearch(
            hosts=[{"host": OPENSEARCH_HOST, "port": 443}],
            http_auth=awsauth,
            use_ssl=True,
            verify_certs=True,  # Always verify TLS certificates
            connection_class=RequestsHttpConnection,
            timeout=CONNECTION_TIMEOUT,
        )
        logger.info(f"OpenSearch client initialized for {OPENSEARCH_HOST}")
    
    return _opensearch_client


# =============================================================================
# Agent Tools - Each tool queries a specific data source
# =============================================================================

@tool
def get_red_metrics(start_time: str = "now-1h", end_time: str = "now") -> dict:
    """
    Get Rate, Error, Duration (RED) metrics aggregated by service.
    
    Use this tool first to identify which services have issues.
    
    Args:
        start_time: Start of time range (default: "now-1h"). 
                   Supports OpenSearch date math: "now-1h", "now-15m", etc.
        end_time: End of time range (default: "now")
    
    Returns:
        Aggregated metrics per service including request count, error count,
        and average duration.
    """
    # Input validation - limit time range to prevent expensive queries
    valid_ranges = ["now-5m", "now-15m", "now-30m", "now-1h", "now-2h", "now-6h", "now-12h", "now-24h"]
    if start_time not in valid_ranges:
        start_time = "now-1h"  # Default to safe value
    
    body = {
        "query": {"range": {"startTime": {"gte": start_time, "lte": end_time}}},
        "aggs": {
            "services": {
                "terms": {"field": "serviceName", "size": 50},
                "aggs": {
                    "request_count": {"value_count": {"field": "traceId"}},
                    "error_count": {"filter": {"term": {"status.code": 2}}},
                    "avg_duration": {"avg": {"field": "durationInNanos"}},
                },
            }
        },
        "size": 0,  # Only return aggregations, not individual documents
    }
    
    return get_opensearch_client().search(index="otel-v1-apm-span-*", body=body)


@tool
def search_logs(
    service: Optional[str] = None,
    severity: Optional[str] = None,
    size: int = 20
) -> dict:
    """
    Search application logs with optional filters.
    
    Use this tool to find error messages and understand what went wrong.
    
    Args:
        service: Filter by service name (e.g., "payment", "checkout")
        severity: Filter by log level: "INFO", "WARN", "ERROR"
        size: Number of results to return (default: 20, max: 100)
    
    Returns:
        Log entries matching the filters, sorted by timestamp descending.
    """
    # Input validation
    size = min(max(1, size), MAX_RESULTS)  # Clamp between 1 and MAX_RESULTS
    
    if severity and severity.upper() not in ["INFO", "WARN", "ERROR", "DEBUG"]:
        severity = None  # Ignore invalid severity
    
    must = [{"range": {"@timestamp": {"gte": "now-1h", "lte": "now"}}}]
    
    if service:
        # Sanitize service name - only allow alphanumeric and hyphens
        service = "".join(c for c in service if c.isalnum() or c == "-")[:50]
        must.append({"term": {"resource.service.name": service}})
    
    if severity:
        must.append({"term": {"severityText": severity.upper()}})
    
    body = {
        "query": {"bool": {"must": must}},
        "sort": [{"@timestamp": "desc"}],
        "size": size,
    }
    
    return get_opensearch_client().search(index="otel-logs-*", body=body)


@tool
def get_spans(
    service_name: Optional[str] = None,
    trace_id: Optional[str] = None,
    size: int = 50
) -> dict:
    """
    Search distributed trace spans to understand request flow.
    
    Use this tool to trace a request across services and find where
    latency or errors originated.
    
    Args:
        service_name: Filter spans by service name
        trace_id: Filter spans by trace ID (get all spans for one request)
        size: Number of results to return (default: 50, max: 100)
    
    Returns:
        Span data including service, operation, duration, and status.
    """
    # Input validation
    size = min(max(1, size), MAX_RESULTS)
    
    must = [{"range": {"startTime": {"gte": "now-1h", "lte": "now"}}}]
    
    if service_name:
        service_name = "".join(c for c in service_name if c.isalnum() or c == "-")[:50]
        must.append({"term": {"serviceName": service_name}})
    
    if trace_id:
        # Trace IDs are hex strings - validate format
        trace_id = "".join(c for c in trace_id if c.isalnum())[:32]
        must.append({"term": {"traceId": trace_id}})
    
    body = {
        "query": {"bool": {"must": must}},
        "sort": [{"startTime": "desc"}],
        "size": size,
    }
    
    return get_opensearch_client().search(index="otel-v1-apm-span-*", body=body)


@tool
def query_metrics(query: str) -> dict:
    """
    Query Prometheus metrics using PromQL.
    
    Use this tool to check infrastructure metrics like CPU, memory,
    and custom application metrics.
    
    Args:
        query: PromQL query expression (e.g., "up", "rate(http_requests_total[5m])")
    
    Returns:
        Prometheus query result with metric values.
    """
    if not AMP_WORKSPACE_ID:
        return {"error": "AMP_WORKSPACE_ID not configured", "status": "error"}
    
    # Input validation - limit query length and sanitize
    query = query[:500]  # Limit query length
    
    url = f"https://aps-workspaces.{AWS_REGION}.amazonaws.com/workspaces/{AMP_WORKSPACE_ID}/api/v1/query"
    params = {"query": query}
    
    # Sign request with SigV4 for AMP authentication
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
# Agent Configuration
# =============================================================================

SYSTEM_PROMPT = """You are an expert Site Reliability Engineer (SRE) investigating incidents.

Your investigation approach:
1. Check RED metrics first (get_red_metrics) to identify problematic services
2. Search logs (search_logs) for error details and patterns
3. Analyze spans (get_spans) to trace request flow and find bottlenecks
4. Query Prometheus metrics (query_metrics) for infrastructure health
5. Provide actionable recommendations with specific next steps

Guidelines:
- Be concise and focus on root cause analysis
- Always cite specific data from your queries
- Prioritize findings by severity
- Suggest both immediate fixes and long-term improvements
"""


@app.entrypoint
def invoke(payload: dict) -> str:
    """
    Handle agent invocation from AgentCore Runtime.
    
    Args:
        payload: Dictionary containing the user prompt.
                 Expected format: {"prompt": "user question here"}
    
    Returns:
        Agent response as a string.
    """
    # Extract prompt from payload (AgentCore passes a dict, not a string)
    prompt = payload.get("prompt", "") if isinstance(payload, dict) else str(payload)
    
    if not prompt:
        return "Please provide a question or investigation request."
    
    logger.info(f"Processing investigation request: {prompt[:100]}...")
    
    agent = Agent(
        model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        system_prompt=SYSTEM_PROMPT,
        tools=[get_red_metrics, search_logs, get_spans, query_metrics],
    )
    
    result = agent(prompt)
    return result.message


if __name__ == "__main__":
    app.run()
