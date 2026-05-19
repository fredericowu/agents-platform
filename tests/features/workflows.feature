Feature: Workflows API
  As a developer
  I want to orchestrate agents using workflows
  So that I can run patterns like orchestrator-worker

  # Real-LLM workflow runs are slow (2-5 min) and cost money. Here we verify
  # the orchestration *shapes* using a workflow wired entirely to echo-coder.
  # End-to-end runs with real LLMs are covered by human spot-check + the demo.

  Scenario: List seeded workflows
    Given the backend is running
    When I list the workflows
    Then I see at least 6 workflows
    And the list contains "orchestrator-worker"
    And the list contains "spec-pipeline"
    And the list contains "build-app"
    And the list contains "enhance-app"

  Scenario: Run the ask-coder (single-stage) workflow as a smoke check
    Given the backend is running
    When I run the workflow "test-echo-pipeline" with input "shape check"
    Then the run completes with status "success"
    And the run output has key "final"
