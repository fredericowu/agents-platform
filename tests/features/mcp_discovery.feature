Feature: MCP discovery from .mcp.json
  The platform reads the workspace .mcp.json and exposes each server.

  Scenario: Workspace MCP servers are visible
    Given the backend is running
    When I refresh the MCP servers
    Then the server list contains "playwright"
    And the server list contains "aw-canvas"
