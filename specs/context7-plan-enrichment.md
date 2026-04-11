# Plan: Context7 Plan-Time Enrichment

## Task Description
Add a Context7 API currency check to the planning phase of the builder/validator pipeline. Context7 lookups happen at plan time — the only non-deterministic node — so the builder and validator remain pure deterministic functions of their inputs.

## Objective
When a plan references external APIs or libraries, Context7 is consulted during plan creation to resolve current/recommended patterns. These verified patterns are written directly into the plan document. The builder follows them verbatim. The validator checks against them. No new non-determinism enters the pipeline.

## Problem Statement
The builder/validator pipeline is designed for determinism:

```
[NON-DETERMINISTIC]                    [DETERMINISTIC]              [DETERMINISTIC]
Plan (human/LLM judgment) ──────────→ Builder (write-only) ──────→ Validator (read-only)
       │                                  │                             │
       │  f(prompt, codebase, judgment)   │  f(plan) → code            │  f(code, plan) → pass/fail
       │  Output varies by run            │  Same plan = same code     │  Same code = same verdict
```

A Context7 check adds value (catches deprecated APIs, outdated patterns) but must not contaminate the deterministic nodes. If placed in the builder or validator, their outputs become functions of external state (`f(plan, docs)` or `f(code, docs)`), breaking the determinism guarantee.

## Solution Approach
**Absorb Context7 into the plan node**, which is already non-deterministic. The plan is where all judgment calls live — which library, which API, which pattern. Adding Context7 here just makes those calls better-informed without changing the determinism properties of any downstream node.

The enriched pipeline:

```
[NON-DETERMINISTIC — already was]       [DETERMINISTIC]              [DETERMINISTIC]
Context7 ─→ Plan (enriched) ──────────→ Builder (write-only) ──────→ Validator (read-only)
   │              │                          │                             │
   │ External     │ f(prompt, codebase,      │ f(plan) → code             │ f(code, plan) → pass/fail
   │ docs lookup  │    judgment, docs)        │ Same plan = same code      │ Same code = same verdict
   │              │ Output varies by run      │                            │
   │              │ (ALREADY DID)             │                            │
```

Key: the dashed boundary around "non-deterministic" doesn't move. Context7 is absorbed into a node that was already non-deterministic. Builder and validator are untouched.

## Relevant Files

- `.claude/commands/plan-w-team.md` — The planning command. **This is where Context7 gets added** as a research step during plan creation. The plan format template inside this file also gets a new "Verified API Patterns" section.
- `.claude/agents/team/builder.md` — Builder agent definition. **No changes.** Remains write-only, deterministic.
- `.claude/agents/team/validator.md` — Validator agent definition. **No changes.** Remains read-only, deterministic.
- `.claude/commands/build.md` — Build orchestrator. **No changes.** Continues to read plan and deploy builder/validator.
- `.claude/commands/verify-changes.md` — Post-pipeline verification. Subagent 3 (Context7) stays as-is — it's an advisory sidecar, not a gate. **No changes needed**, but the plan documents its role for clarity.

### New Files
- `.claude/agents/team/context7-researcher.md` — New agent that performs Context7 lookups during plan creation. Read-only, research-focused.

## Implementation Phases

### Phase 1: Foundation
Create the Context7 researcher agent definition. This is a read-only research agent that:
- Takes a list of APIs/libraries/patterns from the draft plan
- Queries Context7 for current documentation
- Returns verified patterns with version info and any deprecation warnings
- Does NOT modify files — returns findings as structured output

### Phase 2: Core Implementation
Modify `plan-w-team.md` to integrate the Context7 step:
- Add a workflow step between "Design Solution" and "Define Team Members" that spawns the Context7 researcher
- Add a "Verified API Patterns" section to the plan format template
- Add determinism annotations to the Team Orchestration section of the plan template

### Phase 3: Integration & Polish
Verify the full pipeline works end-to-end:
- Plan with Context7 enrichment → Build → Validate
- Confirm builder and validator behavior is unchanged
- Confirm Context7 findings appear in plan documents

## Team Orchestration

- You operate as the team lead and orchestrate the team to execute the plan.
- You're responsible for deploying the right team members with the right context to execute the plan.
- IMPORTANT: You NEVER operate directly on the codebase. You use `Task` and `Task*` tools to deploy team members to do the building, validating, testing, deploying, and other tasks.
  - This is critical. Your job is to act as a high level director of the team, not a builder.
  - Your role is to validate all work is going well and make sure the team is on track to complete the plan.
  - You'll orchestrate this by using the Task* Tools to manage coordination between the team members.
  - Communication is paramount. You'll use the Task* Tools to communicate with the team members and ensure they're on track to complete the plan.
- Take note of the session id of each team member. This is how you'll reference them.

### Team Members

- Builder
  - Name: agent-author
  - Role: Creates the new Context7 researcher agent definition file
  - Agent Type: builder
  - Resume: true

- Builder
  - Name: plan-command-updater
  - Role: Modifies `plan-w-team.md` to add the Context7 workflow step and plan format changes
  - Agent Type: builder
  - Resume: true

- Validator
  - Name: pipeline-integrity-checker
  - Role: Verifies that builder.md and validator.md are unchanged, and that Context7 is only referenced in the plan-time node
  - Agent Type: validator
  - Resume: false

### Pipeline Node Classification

Every node in the pipeline is classified. Builders and validators must not cross the determinism boundary.

| Node | Determinism | Inputs | Output | Can Change? |
|------|------------|--------|--------|-------------|
| Context7 lookup | NON-DETERMINISTIC | API/library names | Current docs/patterns | External state varies |
| Plan creation | NON-DETERMINISTIC | Prompt + codebase + Context7 findings + judgment | Plan document | Already was non-deterministic |
| Builder | DETERMINISTIC | Plan document only | Code changes | **NO — must stay deterministic** |
| Validator | DETERMINISTIC | Code + plan acceptance criteria | Pass/Fail | **NO — must stay deterministic** |
| verify-changes subagent 3 | NON-DETERMINISTIC (advisory) | Finished code | Currency report | Advisory only, does not gate |

## Step by Step Tasks

### 1. Create Context7 Researcher Agent
- **Task ID**: create-context7-agent
- **Depends On**: none
- **Assigned To**: agent-author
- **Agent Type**: builder
- **Parallel**: true
- Create `.claude/agents/team/context7-researcher.md` with the following properties:
  - `name: context7-researcher`
  - `description: Research agent that queries Context7 for current API documentation and patterns. Used at plan time to enrich plans with verified, up-to-date API usage.`
  - `model: sonnet` (research task, doesn't need opus)
  - `disallowedTools: Write, Edit, NotebookEdit` (read-only, like validator)
  - `color: magenta`
- Agent instructions should specify:
  - Accept a list of APIs, libraries, or patterns to verify
  - Use Context7 MCP tools (`resolve-library-id`, `get-library-docs`) to look up current documentation
  - For each item, report: library name, version checked, current recommended pattern, any deprecation warnings
  - Return structured findings as a markdown section that can be pasted into a plan document
  - If Context7 has no data for a library, report "NOT FOUND — manual verification needed"
  - This agent is NON-DETERMINISTIC — its output depends on external doc state. This is acceptable because it only runs at plan time.

### 2. Add Context7 Step to Plan Workflow
- **Task ID**: update-plan-workflow
- **Depends On**: none
- **Assigned To**: plan-command-updater
- **Agent Type**: builder
- **Parallel**: true (independent of task 1)
- In `.claude/commands/plan-w-team.md`, add a new workflow step between step 3 ("Design Solution") and step 4 ("Define Team Members"):
  - New step: "Research API Currency — If the solution involves external APIs or libraries, spawn a `context7-researcher` agent to verify current patterns. Incorporate findings into the plan's Verified API Patterns section."
- In the plan format template, add a new section after "Solution Approach" and before "Relevant Files":
  ```
  ## Verified API Patterns
  <If the plan involves external APIs or libraries, list verified patterns from Context7 research. If no external APIs are used, write "N/A — no external APIs in this plan.">

  | Library/API | Version Checked | Recommended Pattern | Deprecation Warnings |
  |-------------|----------------|--------------------|--------------------|
  | <library>   | <version>      | <pattern>          | <warnings or "none"> |
  ```
- In the plan format template's "Team Orchestration" section, add the pipeline node classification table (from above) as a subsection called "### Pipeline Determinism Map"

### 3. Validate Pipeline Integrity
- **Task ID**: validate-pipeline
- **Depends On**: create-context7-agent, update-plan-workflow
- **Assigned To**: pipeline-integrity-checker
- **Agent Type**: validator
- **Parallel**: false
- Read `.claude/agents/team/builder.md` and verify it has NO references to Context7, no new tools, no changes from current state
- Read `.claude/agents/team/validator.md` and verify it has NO references to Context7, no new tools, no changes from current state
- Read `.claude/commands/build.md` and verify it has NO changes from current state
- Read the new `.claude/agents/team/context7-researcher.md` and verify:
  - It has `disallowedTools: Write, Edit, NotebookEdit`
  - It does NOT have builder capabilities
  - It is classified as a research/read-only agent
- Read the updated `.claude/commands/plan-w-team.md` and verify:
  - Context7 step is in the workflow (plan-time only)
  - The plan format template includes "Verified API Patterns" section
  - The plan format template includes the determinism map
  - No references to Context7 appear in the build/execute sections
- **Key check**: grep all `.claude/agents/team/builder.md` and `.claude/agents/team/validator.md` for "context7" — must return zero matches

## Acceptance Criteria
- [ ] New file `.claude/agents/team/context7-researcher.md` exists and is read-only (disallowedTools includes Write, Edit)
- [ ] `.claude/commands/plan-w-team.md` workflow includes a Context7 research step at plan time
- [ ] Plan format template includes "Verified API Patterns" section
- [ ] Plan format template includes "Pipeline Determinism Map" table
- [ ] `.claude/agents/team/builder.md` is UNCHANGED
- [ ] `.claude/agents/team/validator.md` is UNCHANGED
- [ ] `.claude/commands/build.md` is UNCHANGED
- [ ] No file in the deterministic pipeline (builder, validator, build command) references Context7

## Validation Commands
Execute these commands to validate the task is complete:

```bash
# Verify context7-researcher agent exists and is read-only
grep -c "disallowedTools.*Write" .claude/agents/team/context7-researcher.md
# Expected: 1

# Verify builder is unchanged (no context7 references)
grep -ci "context7" .claude/agents/team/builder.md
# Expected: 0

# Verify validator is unchanged (no context7 references)
grep -ci "context7" .claude/agents/team/validator.md
# Expected: 0

# Verify build command is unchanged (no context7 references)
grep -ci "context7" .claude/commands/build.md
# Expected: 0

# Verify plan-w-team has Context7 in workflow
grep -c "context7-researcher\|Context7\|Verified API Patterns" .claude/commands/plan-w-team.md
# Expected: >= 3

# Verify plan template has determinism map
grep -c "Pipeline Determinism Map\|DETERMINISTIC\|NON-DETERMINISTIC" .claude/commands/plan-w-team.md
# Expected: >= 3
```

## Notes

### Prerequisite: Context7 MCP Server
The Context7 researcher agent requires the Context7 MCP server to be configured. If not already installed, add it to `.claude/settings.json` or the project's MCP config:
```json
{
  "mcpServers": {
    "context7": {
      "command": "npx",
      "args": ["-y", "@anthropic-ai/context7-mcp"]
    }
  }
}
```
If Context7 is unavailable, the researcher agent should gracefully degrade: report "Context7 unavailable — manual verification needed" and allow the plan to proceed without verified patterns.

### Why NOT a post-hook on build
A post-hook on `/build` would run after the builder has already written code. At that point, the code exists and the only options are "rewrite it" or "add a warning." Neither is clean. By running at plan time, the correct patterns are specified before any code is written.

### Relationship to verify-changes subagent 3
`verify-changes.md` subagent 3 remains as-is. It serves a different purpose: post-hoc advisory on already-completed work. The plan-time check ensures new code is written correctly. The verify-time check catches drift in existing code. Both are valuable; neither touches the deterministic pipeline.

### On builder read permissions
The builder agent currently has no `disallowedTools` field. The user's design intent is that the builder operates as a write-focused agent whose output is a pure function of the plan. If enforcing this at the tool level is desired, adding `disallowedTools: Read, Grep, Glob` to `builder.md` would make the constraint explicit. This is a separate concern from the Context7 integration and is noted here for completeness, not as part of this plan's scope.
