import { useReducer, useState } from 'react';
import type { FormEvent } from 'react';

import { approveTask, createTask, rejectTask } from './api';
import type { TaskResponse, WorkspaceEntry } from './api';
import './app.css';
import { initialTaskExecutionState, taskExecutionReducer } from './taskState';

type Decision = 'approve' | 'reject';

interface TaskResultProps {
  task: TaskResponse;
  decisionPending?: Decision | null;
  onDecision?: (decision: Decision) => void;
}

function workspaceEntries(task: TaskResponse): WorkspaceEntry[] {
  const entries = task.execution?.tool_calls[0]?.result?.entries;
  if (!Array.isArray(entries)) {
    return [];
  }
  return entries.filter(
    (entry): entry is WorkspaceEntry =>
      typeof entry === 'object' &&
      entry !== null &&
      typeof (entry as WorkspaceEntry).name === 'string' &&
      ((entry as WorkspaceEntry).kind === 'file' ||
        (entry as WorkspaceEntry).kind === 'directory'),
  );
}

function taskHeading(task: TaskResponse): string {
  switch (task.status) {
    case 'pending':
    case 'planning':
      return 'Planning';
    case 'waiting_for_approval':
      return 'Waiting for approval';
    case 'executing':
      return 'Executing';
    case 'succeeded':
      return 'Task completed';
    case 'rejected':
      return 'Task rejected';
    case 'failed':
      return 'Task failed';
  }
}

function taskMessage(task: TaskResponse): string {
  if (task.status === 'waiting_for_approval') {
    return 'Review the persisted action and arguments before approving or rejecting it.';
  }
  if (task.status === 'rejected') {
    return 'The proposed action was rejected and was not executed.';
  }
  if (task.status === 'executing') {
    return 'The approved action is executing.';
  }
  return task.result ?? task.error ?? 'No final result has been persisted.';
}

export function TaskResult({
  task,
  decisionPending = null,
  onDecision,
}: TaskResultProps) {
  const toolCall = task.execution?.tool_calls[0];
  const approval = task.execution?.approval;
  const entries = workspaceEntries(task);
  const failed = task.status === 'failed';
  const planningStage = task.plan
    ? { state: 'completed', title: 'Plan created', detail: task.plan.summary }
    : failed
      ? {
          state: 'failed',
          title: 'Planning failed',
          detail: task.error ?? 'No plan was persisted.',
        }
      : {
          state: 'pending',
          title: 'Planning',
          detail: 'Waiting for a persisted plan.',
        };
  const approvalStage = approval
    ? approval.decision === 'pending'
      ? {
          state: 'pending',
          title: 'Approval required',
          detail: 'A mutating action is waiting for your decision.',
        }
      : approval.decision === 'approved'
        ? {
            state: 'completed',
            title: 'Action approved',
            detail: 'The persisted action was explicitly approved.',
          }
        : {
            state: 'rejected',
            title: 'Action rejected',
            detail: 'The tool was not executed.',
          }
    : {
        state: task.plan ? 'completed' : 'pending',
        title: task.plan ? 'Read permitted' : 'Permission pending',
        detail: task.plan
          ? 'Read-only actions execute automatically.'
          : 'No permission decision was needed yet.',
      };
  const toolStage = toolCall
    ? toolCall.status === 'succeeded'
      ? {
          state: 'completed',
          title: 'Tool completed',
          detail: toolCall.tool_name,
        }
      : toolCall.status === 'failed'
        ? {
            state: 'failed',
            title: 'Tool failed',
            detail: toolCall.error ?? toolCall.tool_name,
          }
        : toolCall.status === 'rejected'
          ? {
              state: 'rejected',
              title: 'Tool rejected',
              detail: toolCall.tool_name,
            }
          : toolCall.status === 'waiting_for_approval'
            ? {
                state: 'pending',
                title: 'Tool paused',
                detail: toolCall.tool_name,
              }
            : {
                state: 'running',
                title: 'Tool executing',
                detail: toolCall.tool_name,
              }
    : {
        state: 'pending',
        title: failed || task.plan ? 'Tool not started' : 'Tool pending',
        detail: 'No tool call has been persisted.',
      };
  const canDecide =
    task.status === 'waiting_for_approval' &&
    approval?.decision === 'pending' &&
    toolCall?.status === 'waiting_for_approval' &&
    onDecision !== undefined;

  return (
    <section className="result-card" aria-live="polite">
      <div className="result-heading">
        <div>
          <p className="section-label">Latest execution</p>
          <h2>{taskHeading(task)}</h2>
        </div>
        <span className={`status-pill ${task.status}`}>{task.status}</span>
      </div>

      <p className={failed ? 'error-result' : 'final-result'}>
        {taskMessage(task)}
      </p>

      <ol className="execution-steps" aria-label="Execution states">
        <li className="stage-completed">
          <span className="step-index">1</span>
          <div>
            <strong>Task persisted</strong>
            <span>#{task.id.slice(0, 8)}</span>
          </div>
        </li>
        <li className={`stage-${planningStage.state}`}>
          <span className="step-index">2</span>
          <div>
            <strong>{planningStage.title}</strong>
            <span>{planningStage.detail}</span>
          </div>
        </li>
        <li className={`stage-${approvalStage.state}`}>
          <span className="step-index">3</span>
          <div>
            <strong>{approvalStage.title}</strong>
            <span>{approvalStage.detail}</span>
          </div>
        </li>
        <li className={`stage-${toolStage.state}`}>
          <span className="step-index">4</span>
          <div>
            <strong>{toolStage.title}</strong>
            <span>{toolStage.detail}</span>
          </div>
        </li>
      </ol>

      {toolCall && (
        <div className="proposed-action">
          <div className="workspace-result-heading">
            <h3>Selected tool</h3>
            <span>
              {approval ? 'Write · approval required' : 'Read · automatic'}
            </span>
          </div>
          <strong className="tool-name">{toolCall.tool_name}</strong>
          <span className="arguments-label">Requested arguments</span>
          <pre>{JSON.stringify(toolCall.arguments, null, 2)}</pre>
          {canDecide && (
            <div className="approval-controls" aria-label="Approval controls">
              <button
                className="reject-button"
                type="button"
                disabled={decisionPending !== null}
                onClick={() => onDecision('reject')}
              >
                {decisionPending === 'reject' ? 'Rejecting…' : 'Reject'}
              </button>
              <button
                className="approve-button"
                type="button"
                disabled={decisionPending !== null}
                onClick={() => onDecision('approve')}
              >
                {decisionPending === 'approve' ? 'Executing…' : 'Approve'}
              </button>
            </div>
          )}
        </div>
      )}

      {entries.length > 0 && (
        <div className="workspace-result">
          <div className="workspace-result-heading">
            <h3>Workspace entries</h3>
            <span>{entries.length} found</span>
          </div>
          <ul>
            {entries.map((entry) => (
              <li key={`${entry.kind}-${entry.name}`}>
                <span
                  className={`entry-icon ${entry.kind}`}
                  aria-hidden="true"
                />
                <span>{entry.name}</span>
                <small>{entry.kind}</small>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export function App() {
  const [instruction, setInstruction] = useState('List this workspace.');
  const [decisionPending, setDecisionPending] = useState<Decision | null>(null);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [state, dispatch] = useReducer(
    taskExecutionReducer,
    initialTaskExecutionState,
  );
  const isSubmitting = state.phase === 'submitting';
  const isBusy = isSubmitting || decisionPending !== null;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedInstruction = instruction.trim();
    if (!normalizedInstruction || isBusy) {
      return;
    }

    setDecisionError(null);
    dispatch({ type: 'submit' });
    try {
      const task = await createTask(normalizedInstruction);
      dispatch({ type: 'complete', task });
    } catch (error) {
      dispatch({
        type: 'fail',
        message:
          error instanceof Error
            ? error.message
            : 'The task request could not be completed.',
      });
    }
  }

  async function handleDecision(decision: Decision) {
    if (state.phase !== 'completed' || decisionPending !== null) {
      return;
    }
    const task = state.task;
    setDecisionError(null);
    setDecisionPending(decision);
    try {
      const decidedTask =
        decision === 'approve'
          ? await approveTask(task.id)
          : await rejectTask(task.id);
      dispatch({ type: 'complete', task: decidedTask });
    } catch (error) {
      setDecisionError(
        error instanceof Error
          ? error.message
          : 'The approval decision could not be completed.',
      );
    } finally {
      setDecisionPending(null);
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="AURA home">
          <span className="brand-mark">A</span>
          <span>AURA</span>
        </a>
        <span className="phase-badge">Phase 3 · guarded tools</span>
      </header>

      <main>
        <section className="task-intro" aria-labelledby="task-title">
          <div className="intro-copy">
            <p className="eyebrow">Bounded agent workflow</p>
            <h1 id="task-title">Work in the workspace safely.</h1>
            <p className="summary">
              The planner selects one registered tool. Reads run automatically;
              moves pause until you explicitly approve or reject the persisted
              action.
            </p>
          </div>

          <form className="task-form" onSubmit={handleSubmit}>
            <label htmlFor="instruction">Task instruction</label>
            <textarea
              id="instruction"
              name="instruction"
              rows={4}
              maxLength={2000}
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              disabled={isBusy}
            />
            <div className="form-footer">
              <span>One validated tool call · explicit write approval</span>
              <button type="submit" disabled={!instruction.trim() || isBusy}>
                {isSubmitting ? 'Planning…' : 'Run task'}
              </button>
            </div>
          </form>
        </section>

        {isSubmitting && (
          <section className="pending-card" aria-live="polite">
            <span className="pending-indicator" aria-hidden="true" />
            <div>
              <strong>Planning</strong>
              <p>The plan is being validated and persisted.</p>
            </div>
          </section>
        )}

        {decisionPending && (
          <section className="pending-card" aria-live="polite">
            <span className="pending-indicator" aria-hidden="true" />
            <div>
              <strong>
                {decisionPending === 'approve'
                  ? 'Executing approved action'
                  : 'Recording rejection'}
              </strong>
              <p>
                Duplicate decisions are disabled while this request completes.
              </p>
            </div>
          </section>
        )}

        {state.phase === 'request-failed' && (
          <section className="request-error" role="alert">
            <strong>API request failed</strong>
            <p>{state.message}</p>
          </section>
        )}

        {decisionError && (
          <section className="request-error" role="alert">
            <strong>Approval request failed</strong>
            <p>{decisionError}</p>
          </section>
        )}

        {state.phase === 'completed' && (
          <TaskResult
            task={state.task}
            decisionPending={decisionPending}
            onDecision={handleDecision}
          />
        )}
      </main>
    </div>
  );
}

export default App;
