import { useReducer, useState } from 'react';
import type { FormEvent } from 'react';

import { createTask } from './api';
import type { TaskResponse } from './api';
import './app.css';
import { initialTaskExecutionState, taskExecutionReducer } from './taskState';

interface TaskResultProps {
  task: TaskResponse;
}

export function TaskResult({ task }: TaskResultProps) {
  const toolCall = task.execution?.tool_calls[0];
  const entries = toolCall?.result?.entries ?? [];
  const succeeded = task.status === 'succeeded';
  const failed = task.status === 'failed';
  const heading = succeeded
    ? 'Task completed'
    : failed
      ? 'Task failed'
      : 'Task in progress';
  const planningStage = task.plan
    ? {
        state: 'completed',
        title: 'Plan created',
        detail: task.plan.summary,
      }
    : failed
      ? {
          state: 'failed',
          title: 'Planning failed',
          detail: task.error ?? 'No plan was persisted.',
        }
      : {
          state: 'pending',
          title: 'Planning pending',
          detail: 'No plan has been persisted yet.',
        };
  const toolStage = toolCall
    ? toolCall.status === 'succeeded'
      ? {
          state: 'completed',
          title: 'Read-only tool completed',
          detail: toolCall.tool_name,
        }
      : toolCall.status === 'failed'
        ? {
            state: 'failed',
            title: 'Tool failed',
            detail: toolCall.error ?? toolCall.tool_name,
          }
        : {
            state: 'running',
            title: 'Tool running',
            detail: toolCall.tool_name,
          }
    : {
        state: 'pending',
        title: failed || task.plan ? 'Tool not started' : 'Tool pending',
        detail: 'No tool call has been persisted.',
      };

  return (
    <section className="result-card" aria-live="polite">
      <div className="result-heading">
        <div>
          <p className="section-label">Latest execution</p>
          <h2>{heading}</h2>
        </div>
        <span className={`status-pill ${task.status}`}>{task.status}</span>
      </div>

      <p className={failed ? 'error-result' : 'final-result'}>
        {task.result ?? task.error ?? 'No final result has been persisted.'}
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
        <li className={`stage-${toolStage.state}`}>
          <span className="step-index">3</span>
          <div>
            <strong>{toolStage.title}</strong>
            <span>{toolStage.detail}</span>
          </div>
        </li>
      </ol>

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
  const [state, dispatch] = useReducer(
    taskExecutionReducer,
    initialTaskExecutionState,
  );
  const isSubmitting = state.phase === 'submitting';

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedInstruction = instruction.trim();
    if (!normalizedInstruction || isSubmitting) {
      return;
    }

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

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="AURA home">
          <span className="brand-mark">A</span>
          <span>AURA</span>
        </a>
        <span className="phase-badge">Phase 2 · provider-ready</span>
      </header>

      <main>
        <section className="task-intro" aria-labelledby="task-title">
          <div className="intro-copy">
            <p className="eyebrow">Bounded agent workflow</p>
            <h1 id="task-title">Inspect the workspace safely.</h1>
            <p className="summary">
              Create one task. The configured planner will select AURA&apos;s
              registered read-only tool, persist the execution, and return the
              result.
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
              disabled={isSubmitting}
            />
            <div className="form-footer">
              <span>Runs workspace_list · read only</span>
              <button
                type="submit"
                disabled={!instruction.trim() || isSubmitting}
              >
                {isSubmitting ? 'Executing…' : 'Run task'}
              </button>
            </div>
          </form>
        </section>

        {isSubmitting && (
          <section className="pending-card" aria-live="polite">
            <span className="pending-indicator" aria-hidden="true" />
            <div>
              <strong>Planning and executing</strong>
              <p>The task and its operational state are being persisted.</p>
            </div>
          </section>
        )}

        {state.phase === 'request-failed' && (
          <section className="request-error" role="alert">
            <strong>API request failed</strong>
            <p>{state.message}</p>
          </section>
        )}

        {state.phase === 'completed' && <TaskResult task={state.task} />}
      </main>
    </div>
  );
}

export default App;
