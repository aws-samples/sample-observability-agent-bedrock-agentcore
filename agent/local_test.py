# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Observability Agent - Local testing version.

This script allows testing the agent locally before deploying to AgentCore.
It uses the same tools as the deployed agent but runs in your local environment.

Usage:
    export OPENSEARCH_HOST=your-collection-id.us-east-1.aoss.amazonaws.com
    python agent/local_test.py
"""

import os
import sys

# Validate required configuration
if not os.environ.get("OPENSEARCH_HOST"):
    print("ERROR: OPENSEARCH_HOST environment variable is required")
    print("Example: export OPENSEARCH_HOST=abc123.us-east-1.aoss.amazonaws.com")
    sys.exit(1)

from strands import Agent

SYSTEM_PROMPT = """You are an expert Site Reliability Engineer (SRE) investigating incidents.

Your approach:
1. Start with high-level health checks (RED metrics)
2. Narrow down to affected services based on error rates and latency
3. Correlate logs and traces to find root cause
4. Provide actionable recommendations

When investigating:
- Check RED metrics first to identify problematic services
- Use trace IDs to correlate logs across services
- Look for error patterns and anomalies
- Consider service dependencies when analyzing impact

Always explain your reasoning and suggest next steps."""

# Import tools from MCP server modules
from mcp_servers.logs_server import search_otel_logs, get_logs_by_trace_id
from mcp_servers.traces_server import (
    get_otel_spans,
    get_spans_by_trace_id,
    get_otel_service_map,
    get_otel_red_metrics,
)

# Create agent with tools
agent = Agent(
    model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    system_prompt=SYSTEM_PROMPT,
    tools=[
        search_otel_logs,
        get_logs_by_trace_id,
        get_otel_spans,
        get_spans_by_trace_id,
        get_otel_service_map,
        get_otel_red_metrics,
    ],
)


def main():
    """Interactive CLI for testing the observability agent."""
    print("Observability Agent - Local Test Mode")
    print("=" * 50)
    print("Example queries:")
    print("  - Are there any errors in my application?")
    print("  - What's wrong with the payment service?")
    print("  - Show me the service dependencies")
    print("Type 'quit' or 'exit' to stop.")
    print("=" * 50)
    
    while True:
        try:
            query = input("\nYou: ").strip()
            if not query:
                continue
            if query.lower() in ("quit", "exit", "q"):
                print("Goodbye!")
                break
            
            print("\nAgent: ", end="", flush=True)
            result = agent(query)
            print(result.message)
        except KeyboardInterrupt:
            print("\nGoodbye!")
            break
        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    main()
