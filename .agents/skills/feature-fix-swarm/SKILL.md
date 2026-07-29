```markdown
# feature-fix-swarm Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill teaches the core development patterns and conventions used in the `feature-fix-swarm` Python repository. You'll learn about file naming, import/export styles, commit message conventions, and how to structure and run tests. While no specific automation workflows were detected, this guide provides best practices and suggested commands to streamline your development process.

## Coding Conventions

### File Naming
- Use **camelCase** for file names.
  - Example: `featureFixSwarm.py`, `userManager.py`

### Import Style
- Use **relative imports** within the package.
  - Example:
    ```python
    from .utils import parseFeature
    from .models import SwarmModel
    ```

### Export Style
- Use **named exports** (i.e., explicitly define what is exported from a module).
  - Example:
    ```python
    # In featureFixSwarm.py
    def fixSwarmIssue(...):
        ...

    __all__ = ['fixSwarmIssue']
    ```

### Commit Messages
- Use the **Conventional Commits** pattern.
  - Prefix commit messages with `feat`.
  - Keep messages concise (average 67 characters).
  - Example:
    ```
    feat: add support for dynamic swarm resizing
    ```

## Workflows

_No automated workflows were detected in this repository. Below are suggested manual workflows based on common development tasks._

### Feature Development
**Trigger:** When adding a new feature  
**Command:** `/feature-dev`

1. Create a new branch:  
   `git checkout -b feat/short-description`
2. Implement the feature using camelCase file naming and relative imports.
3. Write or update tests as needed.
4. Commit changes with a conventional commit message:  
   `git commit -m "feat: brief description of the feature"`
5. Push the branch and open a pull request.

### Bug Fixing
**Trigger:** When fixing a bug  
**Command:** `/bug-fix`

1. Create a new branch:  
   `git checkout -b fix/short-description`
2. Apply the fix, following the coding conventions.
3. Add or update tests to cover the fix.
4. Commit with a conventional commit message:  
   `git commit -m "fix: brief description of the bug fix"`
5. Push and open a pull request.

### Testing
**Trigger:** Before merging or releasing  
**Command:** `/run-tests`

1. Locate test files (pattern: `*.test.ts`).
2. Run tests using the appropriate test runner (framework is unknown; adapt as needed).
3. Ensure all tests pass before merging.

## Testing Patterns

- Test files follow the `*.test.ts` pattern (TypeScript).
- The specific test framework is unknown; adapt commands to your environment.
- Example test file name: `featureFixSwarm.test.ts`

## Commands

| Command        | Purpose                                         |
|----------------|-------------------------------------------------|
| /feature-dev   | Start a new feature development workflow         |
| /bug-fix       | Start a new bug fix workflow                    |
| /run-tests     | Run all tests before merging or releasing       |
```