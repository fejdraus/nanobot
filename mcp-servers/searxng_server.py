#!/usr/bin/env python3
"""MCP Server for SearXNG web search."""

import json
import sys
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# SearXNG endpoint
SEARXNG_URL = "http://192.168.1.60:8888/search"

server = Server("searxng")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="web_search",
            description="Search the web using SearXNG. Returns titles, URLs, and snippets.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query"
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results (1-10)",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Execute a tool."""
    if name != "web_search":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    
    query = arguments.get("query", "")
    count = min(max(arguments.get("count", 5), 1), 10)
    
    if not query:
        return [TextContent(type="text", text="Error: query is required")]
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                SEARXNG_URL,
                params={"q": query, "format": "json"}
            )
            response.raise_for_status()
            data = response.json()
        
        results = data.get("results", [])[:count]
        
        if not results:
            return [TextContent(type="text", text=f"No results for: {query}")]
        
        lines = [f"Results for: {query}\n"]
        for i, item in enumerate(results, 1):
            title = item.get("title", "No title")
            url = item.get("url", "")
            snippet = item.get("content", "")
            lines.append(f"{i}. {title}")
            lines.append(f"   {url}")
            if snippet:
                lines.append(f"   {snippet[:200]}")
            lines.append("")
        
        return [TextContent(type="text", text="\n".join(lines))]
    
    except httpx.HTTPError as e:
        return [TextContent(type="text", text=f"HTTP error: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
