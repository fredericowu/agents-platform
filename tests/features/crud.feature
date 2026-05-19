Feature: CRUD for models, MCP servers, evals
  Verify the new write APIs round-trip cleanly.

  Scenario: Create + update + delete a Model
    Given the backend is running
    When I create a model with slug "bdd-test-model" provider "echo"
    Then the model "bdd-test-model" exists in the list
    When I disable the model "bdd-test-model"
    Then the model "bdd-test-model" is disabled
    When I delete the model "bdd-test-model"
    Then the model "bdd-test-model" is not in the list

  Scenario: Create + delete a custom MCP server
    Given the backend is running
    When I add an MCP server "bdd-mcp" with command "echo"
    Then the MCP server "bdd-mcp" exists in the list
    And the MCP server "bdd-mcp" has source "manual"
    When I delete the MCP server "bdd-mcp"
    Then the MCP server "bdd-mcp" is not in the list

  Scenario: Create + delete an eval
    Given the backend is running
    When I create an eval with slug "bdd-test-eval"
    Then the eval "bdd-test-eval" exists in the list
    When I delete the eval "bdd-test-eval"
    Then the eval "bdd-test-eval" is not in the list
