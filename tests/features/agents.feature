Feature: Agents API
  As a developer
  I want to manage and run agents
  So that I can compose them into workflows

  Scenario: List seeded agents
    Given the backend is running
    When I list the agents
    Then I see at least 13 agents
    And the list contains "coder"
    And the list contains "planner"
    And the list contains "echo-coder"

  Scenario: Run the echo agent and get the input echoed back
    Given the backend is running
    When I run the agent "echo-coder" with input "hello bdd"
    Then the run completes with status "success"
    And the run output text contains "hello bdd"
    And the run has tokens recorded
