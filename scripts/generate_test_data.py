# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Generate and ingest test observability data into OpenSearch Serverless.

This script creates sample logs and traces simulating a payment service failure,
useful for testing the observability agent without real application data.

Usage:
    export OPENSEARCH_HOST=your-collection-id.us-east-1.aoss.amazonaws.com
    python scripts/generate_test_data.py
"""

import random
import uuid
from datetime import datetime, timedelta, UTC
import os
import sys

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

# =============================================================================
# Configuration - Set via environment variables
# =============================================================================
OPENSEARCH_HOST = os.environ.get("OPENSEARCH_HOST", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

if not OPENSEARCH_HOST:
    print("ERROR: OPENSEARCH_HOST environment variable is required")
    print("Example: export OPENSEARCH_HOST=abc123.us-east-1.aoss.amazonaws.com")
    sys.exit(1)

# Services in our mock e-commerce application
SERVICES = ["frontend", "checkout", "payment", "inventory", "shipping"]


def get_opensearch_client() -> OpenSearch:
    """Create OpenSearch client with AWS SigV4 authentication."""
    session = boto3.Session()
    credentials = session.get_credentials()
    
    awsauth = AWS4Auth(
        credentials.access_key,
        credentials.secret_key,
        AWS_REGION,
        "aoss",
        session_token=credentials.token,
    )
    
    return OpenSearch(
        hosts=[{"host": OPENSEARCH_HOST, "port": 443}],
        http_auth=awsauth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=60,
    )


def create_indices(client: OpenSearch) -> None:
    """Create OpenSearch indices for logs and traces."""
    today = datetime.now(UTC).strftime("%Y.%m.%d")
    
    logs_mapping = {
        "mappings": {
            "properties": {
                "@timestamp": {"type": "date"},
                "body": {"type": "text"},
                "severityText": {"type": "keyword"},
                "resource.service.name": {"type": "keyword"},
                "traceId": {"type": "keyword"},
                "spanId": {"type": "keyword"},
            }
        }
    }
    
    traces_mapping = {
        "mappings": {
            "properties": {
                "traceId": {"type": "keyword"},
                "spanId": {"type": "keyword"},
                "parentSpanId": {"type": "keyword"},
                "serviceName": {"type": "keyword"},
                "name": {"type": "keyword"},
                "startTime": {"type": "date"},
                "endTime": {"type": "date"},
                "durationInNanos": {"type": "long"},
                "status.code": {"type": "integer"},
                "destination.service.resource": {"type": "keyword"},
            }
        }
    }
    
    for index_name, mapping in [
        (f"otel-logs-{today}", logs_mapping),
        (f"otel-v1-apm-span-{today}", traces_mapping),
    ]:
        try:
            client.indices.create(index=index_name, body=mapping)
            print(f"Created index: {index_name}")
        except Exception as e:
            if "resource_already_exists_exception" in str(e):
                print(f"Index already exists: {index_name}")
            else:
                print(f"Error creating {index_name}: {e}")


def generate_trace(base_time: datetime, has_error: bool = False) -> tuple[list, list]:
    """
    Generate a complete distributed trace with spans and logs.
    
    Args:
        base_time: Timestamp for the trace
        has_error: If True, simulate a payment service failure
    
    Returns:
        Tuple of (spans list, logs list)
    """
    trace_id = uuid.uuid4().hex
    spans = []
    logs = []
    
    # Frontend span (root)
    frontend_span_id = uuid.uuid4().hex[:16]
    frontend_duration = random.randint(100, 500) * 1_000_000  # Convert ms to ns
    
    spans.append({
        "traceId": trace_id,
        "spanId": frontend_span_id,
        "parentSpanId": "",
        "serviceName": "frontend",
        "name": "HTTP GET /checkout",
        "startTime": base_time.isoformat(),
        "durationInNanos": frontend_duration,
        "status.code": 0,
    })
    
    logs.append({
        "@timestamp": base_time.isoformat(),
        "body": "Received checkout request",
        "severityText": "INFO",
        "resource.service.name": "frontend",
        "traceId": trace_id,
    })
    
    # Checkout span
    checkout_span_id = uuid.uuid4().hex[:16]
    checkout_start = base_time + timedelta(milliseconds=10)
    
    spans.append({
        "traceId": trace_id,
        "spanId": checkout_span_id,
        "parentSpanId": frontend_span_id,
        "serviceName": "checkout",
        "name": "ProcessCheckout",
        "startTime": checkout_start.isoformat(),
        "durationInNanos": random.randint(50, 200) * 1_000_000,
        "status.code": 0,
    })
    
    # Payment span (where errors occur)
    payment_span_id = uuid.uuid4().hex[:16]
    payment_start = checkout_start + timedelta(milliseconds=20)
    payment_duration = 30_000_000_000 if has_error else random.randint(100, 300) * 1_000_000
    
    spans.append({
        "traceId": trace_id,
        "spanId": payment_span_id,
        "parentSpanId": checkout_span_id,
        "serviceName": "payment",
        "name": "ProcessPayment",
        "startTime": payment_start.isoformat(),
        "durationInNanos": payment_duration,
        "status.code": 2 if has_error else 0,  # 2 = ERROR in OpenTelemetry
    })
    
    if has_error:
        logs.append({
            "@timestamp": payment_start.isoformat(),
            "body": "ERROR: Connection refused to payment gateway at 10.0.1.50:443 - Connection timed out after 30000ms",
            "severityText": "ERROR",
            "resource.service.name": "payment",
            "traceId": trace_id,
        })
    else:
        logs.append({
            "@timestamp": payment_start.isoformat(),
            "body": "Payment processed successfully",
            "severityText": "INFO",
            "resource.service.name": "payment",
            "traceId": trace_id,
        })
    
    return spans, logs


def ingest_data(client: OpenSearch) -> None:
    """Generate and ingest test data simulating a payment service incident."""
    now = datetime.now(UTC)
    today = now.strftime("%Y.%m.%d")
    all_spans = []
    all_logs = []
    
    # Generate normal traffic (past hour)
    print("Generating normal traffic...")
    for _ in range(50):
        base_time = now - timedelta(minutes=random.randint(10, 60))
        spans, logs = generate_trace(base_time, has_error=False)
        all_spans.extend(spans)
        all_logs.extend(logs)
    
    # Generate error traffic (last 5 minutes - simulating incident)
    print("Generating error traffic (payment service incident)...")
    for _ in range(30):
        base_time = now - timedelta(minutes=random.randint(0, 5))
        spans, logs = generate_trace(base_time, has_error=True)
        all_spans.extend(spans)
        all_logs.extend(logs)
    
    # Bulk index in batches
    batch_size = 50
    
    print(f"Indexing {len(all_spans)} spans...")
    for i in range(0, len(all_spans), batch_size):
        batch = all_spans[i:i + batch_size]
        bulk_body = []
        for span in batch:
            bulk_body.extend([{"index": {"_index": f"otel-v1-apm-span-{today}"}}, span])
        response = client.bulk(body=bulk_body)
        print(f"  Batch {i // batch_size + 1}: errors={response.get('errors', False)}")
    
    print(f"Indexing {len(all_logs)} logs...")
    for i in range(0, len(all_logs), batch_size):
        batch = all_logs[i:i + batch_size]
        bulk_body = []
        for log in batch:
            bulk_body.extend([{"index": {"_index": f"otel-logs-{today}"}}, log])
        response = client.bulk(body=bulk_body)
        print(f"  Batch {i // batch_size + 1}: errors={response.get('errors', False)}")


def main():
    """Main entry point."""
    print(f"Connecting to OpenSearch: {OPENSEARCH_HOST}")
    client = get_opensearch_client()
    
    print("\nCreating indices...")
    create_indices(client)
    
    print("\nIngesting test data...")
    ingest_data(client)
    
    print("\nDone! Test data has been ingested.")
    print("You can now test the agent with: agentcore invoke '{\"prompt\": \"Are there any errors?\"}'")


if __name__ == "__main__":
    main()
