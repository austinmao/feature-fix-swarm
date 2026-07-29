```markdown
# feature-fix-swarm Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill teaches you the core development patterns and conventions used in the `feature-fix-swarm` Python repository. You'll learn about file naming, import/export styles, commit message conventions, and how to structure and run tests. This guide is ideal for contributors who want to quickly align with the project's established practices.

## Coding Conventions

### File Naming
- Use **camelCase** for file names.
  - Example: `featureFixSwarm.py`, `dataProcessor.py`

### Import Style
- Use **relative imports** within the codebase.
  - Example:
    ```python
    from .utils import parseData
    from ..models import SwarmModel
    ```

### Export Style
- Use **named exports** (explicitly define what is exported).
  - Example:
    ```python
    def processSwarm():
        pass

    __all__ = ['processSwarm']
    ```

### Commit Messages
- Follow the **conventional commit** style.
- Use the `feat` prefix for new features or fixes.
- Keep commit messages concise (average 80 characters).
  - Example:
    ```
    feat: improve swarm node coordination logic
    ```

## Workflows

_No automated workflows detected in the repository._

## Testing Patterns

- **Test files** use the `*.test.*` naming pattern.
  - Example: `swarmManager.test.py`
- **Testing framework** is not specified; check individual test files for details.
- To run tests, locate files matching the `*.test.*` pattern and execute them with your preferred Python test runner (e.g., `pytest`, `unittest`).

  Example test file:
  ```python
  # swarmManager.test.py
  from .swarmManager import SwarmManager

  def test_swarm_initialization():
      swarm = SwarmManager()
      assert swarm.is_initialized()
  ```

## Commands
| Command   | Purpose                                 |
|-----------|-----------------------------------------|
| /test     | Run all test files in the repository    |
| /lint     | Check code style and formatting         |
| /commit   | Create a conventional commit message    |
```