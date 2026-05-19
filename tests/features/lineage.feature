Feature: Run lineage
  Workflow runs spawn child agent runs; the tree endpoint returns the chain.

  Scenario: Workflow run produces children with parent_run_id set
    Given the backend is running
    When I run the workflow "test-echo-parallel" with input "lineage"
    Then the run completes with status "success"
    And the run tree includes at least 4 total runs
    And the run tree has at least 3 child runs
    And every child run has parent_run_id equal to the workflow run id
