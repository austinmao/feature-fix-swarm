```markdown
# feature-fix-swarm Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill teaches the core development patterns and conventions used in the `feature-fix-swarm` TypeScript codebase. It covers file naming, import/export styles, commit message standards, and testing patterns. While no automated workflows were detected, this guide provides recommended commands and step-by-step instructions for common development tasks.

## Coding Conventions

### File Naming
- **Pattern:** PascalCase
- **Example:**  
  ```text
  FeatureComponent.ts
  UserService.ts
  ```

### Import Style
- **Pattern:** Relative imports
- **Example:**  
  ```typescript
  import { UserService } from './UserService';
  ```

### Export Style
- **Pattern:** Named exports
- **Example:**  
  ```typescript
  export function calculateSum(a: number, b: number): number {
    return a + b;
  }
  ```

### Commit Messages
- **Pattern:** Conventional Commits
- **Prefix:** `feat`
- **Average Length:** ~57 characters
- **Example:**  
  ```
  feat: add user authentication to login form
  ```

## Workflows

### Creating a New Feature
**Trigger:** When adding new functionality  
**Command:** `/new-feature`

1. Create a new file using PascalCase (e.g., `NewFeature.ts`).
2. Use relative imports to include dependencies.
3. Export your functions or classes using named exports.
4. Write a commit message starting with `feat:` and a concise description.

### Fixing a Bug
**Trigger:** When resolving a bug or issue  
**Command:** `/fix-bug`

1. Locate the relevant file(s) using PascalCase naming.
2. Make the necessary code changes.
3. Ensure imports and exports follow the conventions.
4. Write a commit message starting with `feat:` and a clear description of the fix.

### Writing and Running Tests
**Trigger:** When adding or updating tests  
**Command:** `/run-tests`

1. Create or update test files matching the `*.test.*` pattern (e.g., `FeatureComponent.test.ts`).
2. Write tests for your functions or classes.
3. Use the project's test runner (framework unknown; check project documentation or package.json).
4. Run the tests to verify correctness.

## Testing Patterns

- **Test File Naming:** Files should match the pattern `*.test.*` (e.g., `UserService.test.ts`).
- **Framework:** Not explicitly detected; refer to project documentation.
- **Placement:** Test files are typically located alongside the code they test or in a dedicated test directory.

**Example:**
```typescript
import { calculateSum } from './calculateSum';

test('adds two numbers', () => {
  expect(calculateSum(2, 3)).toBe(5);
});
```

## Commands
| Command         | Purpose                                |
|-----------------|----------------------------------------|
| /new-feature    | Start a new feature implementation     |
| /fix-bug        | Begin work on a bug fix                |
| /run-tests      | Run the test suite                     |
```
