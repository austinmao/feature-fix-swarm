```markdown
# feature-fix-swarm Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill teaches the core development patterns and conventions used in the `feature-fix-swarm` TypeScript repository. You'll learn how to structure files, write imports and exports, follow commit message guidelines, and organize tests. This guide is ideal for contributors aiming to maintain consistency and quality in this codebase.

## Coding Conventions

### File Naming
- Use **kebab-case** for all file and directory names.
  - Example:  
    ```
    user-profile.ts
    api-client.test.ts
    ```

### Imports
- Use **relative imports** for referencing other modules.
  - Example:
    ```typescript
    import { fetchData } from './api-client';
    ```

### Exports
- Use **named exports** instead of default exports.
  - Example:
    ```typescript
    // Good
    export function fetchData() { ... }

    // Bad
    export default function fetchData() { ... }
    ```

### Commit Messages
- Follow the **Conventional Commits** format.
- Use the `feat` prefix for new features or fixes.
- Keep commit messages concise (average ~60 characters).
  - Example:
    ```
    feat: add user authentication to login flow
    ```

## Workflows

### Feature or Fix Development
**Trigger:** When adding a new feature or fixing a bug  
**Command:** `/feature-fix`

1. Create a new branch using kebab-case (e.g., `feat-add-user-auth`).
2. Implement your changes following the coding conventions.
3. Write or update tests in a corresponding `*.test.*` file.
4. Stage and commit your changes using the conventional commit format.
5. Push your branch and open a pull request.

### Testing
**Trigger:** When verifying code functionality  
**Command:** `/test`

1. Identify or create test files matching the `*.test.*` pattern.
2. Run the project's test runner (framework is unknown; check project docs or use a common runner like `npm test`).
3. Review test results and fix any failing tests before merging.

## Testing Patterns

- Test files should follow the `*.test.*` naming convention.
  - Example:  
    ```
    api-client.test.ts
    ```
- Place test files alongside the modules they test or in a dedicated test directory.
- The testing framework is not specified; check project documentation or existing test files for guidance.

## Commands
| Command        | Purpose                                      |
|----------------|----------------------------------------------|
| /feature-fix   | Start a new feature or bugfix workflow       |
| /test          | Run the test suite and verify code correctness|
```