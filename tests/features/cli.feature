Feature: agent-cli
  The CLI wraps the backend API.

  Scenario: list agents via CLI
    Given the backend is running
    When I run "agent list agents"
    Then the command exits zero
    And stdout contains "coder"
    And stdout contains "planner"

  Scenario: run an agent via CLI (using fast echo-coder)
    Given the backend is running
    When I run "agent run echo-coder -i hello-from-cli"
    Then the command exits zero
    And stdout contains "hello-from-cli"

  Scenario: run a workflow via CLI (using fast echo pipeline)
    Given the backend is running
    When I run "agent run-wf test-echo-parallel -i sweep"
    Then the command exits zero
    And stdout contains "outputs"
