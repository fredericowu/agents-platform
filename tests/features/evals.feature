Feature: Evals
  Score agents/workflows against a dataset.

  Scenario: Echo-smoke eval scores 100%
    Given the backend is running
    When I run the eval "echo-smoke"
    Then the eval score is 1.0
    And all eval cases pass
