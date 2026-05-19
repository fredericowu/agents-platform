Feature: agent-mcp
  The MCP server exposes agents and workflows as MCP tools.

  Scenario: Tools include introspection + each agent + each workflow
    Given the backend is running
    When I connect to agent-mcp
    Then the tool list includes "list_agents"
    And the tool list includes "list_workflows"
    And the tool list includes "agent_coder"
    And the tool list includes "workflow_orchestrator_worker"

  Scenario: Calling an agent tool via MCP returns the agent output (fast, echo)
    Given the backend is running
    When I call MCP tool "agent_echo_coder" with input "mcp-bdd-test"
    Then the result contains "mcp-bdd-test"

  Scenario: Calling a workflow tool via MCP returns the workflow output (fast, echo)
    Given the backend is running
    When I call MCP tool "workflow_test_echo_parallel" with input "via-mcp-bdd"
    Then the result contains "outputs"
