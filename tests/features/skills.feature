Feature: Skills discovery
  The platform reads skills from .claude/skills.

  Scenario: Workspace skills are visible
    Given the backend is running
    When I list the skills
    Then the skills list contains "dt-loco-stubborn"
    And the skills list contains "toggle-sweep-ai"
