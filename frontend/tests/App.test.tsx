import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { App, TaskResult } from '../src/App';
import { approveTask, createTask, rejectTask } from '../src/api';
import type { TaskResponse } from '../src/api';
import {
  initialTaskExecutionState,
  taskExecutionReducer,
} from '../src/taskState';

const completedTask: TaskResponse = {
  id: 'efdbf199-3630-484c-8dfb-49b18d0ae32f',
  instruction: 'List this workspace.',
  status: 'succeeded',
  result: 'Found 2 top-level entries in the configured workspace.',
  error: null,
  created_at: '2026-09-11T15:00:00Z',
  updated_at: '2026-09-11T15:00:01Z',
  plan: {
    id: '38ba8843-77bb-414c-a6f9-74dcb3618e31',
    planner: 'mock-planner-v1',
    summary: 'Inspect the configured workspace for "List this workspace.".',
    created_at: '2026-09-11T15:00:00Z',
    steps: [
      {
        sequence: 1,
        title: 'List top-level workspace entries',
        tool_name: 'workspace_list',
        arguments: {},
      },
    ],
  },
  execution: {
    id: '85bf382f-c8b5-4cb3-b3ac-2adb2b0d0e1e',
    status: 'succeeded',
    result: {
      workspace: 'project_aura',
      entry_count: 2,
      summary: 'Found 2 top-level entries in the configured workspace.',
      entries: [
        { name: 'backend', kind: 'directory' },
        { name: 'README.md', kind: 'file' },
      ],
    },
    error: null,
    started_at: '2026-09-11T15:00:00Z',
    completed_at: '2026-09-11T15:00:01Z',
    tool_calls: [
      {
        id: '38af2a77-e4c3-4ac5-b688-aaf99f5edbb3',
        tool_name: 'workspace_list',
        arguments: {},
        status: 'succeeded',
        result: {
          workspace: 'project_aura',
          entry_count: 2,
          summary: 'Found 2 top-level entries in the configured workspace.',
          entries: [
            { name: 'backend', kind: 'directory' },
            { name: 'README.md', kind: 'file' },
          ],
        },
        error: null,
        started_at: '2026-09-11T15:00:00Z',
        completed_at: '2026-09-11T15:00:01Z',
      },
    ],
    approval: null,
  },
};

const waitingTask: TaskResponse = {
  ...completedTask,
  status: 'waiting_for_approval',
  result: null,
  plan: {
    ...completedTask.plan!,
    summary: 'Move source.txt to moved.txt after approval.',
    steps: [
      {
        sequence: 1,
        title: 'Move one workspace file',
        tool_name: 'workspace_move',
        arguments: { source: 'source.txt', destination: 'moved.txt' },
      },
    ],
  },
  execution: {
    ...completedTask.execution!,
    status: 'waiting_for_approval',
    result: null,
    completed_at: null,
    tool_calls: [
      {
        ...completedTask.execution!.tool_calls[0],
        tool_name: 'workspace_move',
        arguments: { source: 'source.txt', destination: 'moved.txt' },
        status: 'waiting_for_approval',
        result: null,
        completed_at: null,
      },
    ],
    approval: {
      id: '124191cb-522a-4f1c-92a7-c1398b3199e8',
      decision: 'pending',
      requested_at: '2026-09-11T15:00:00Z',
      decided_at: null,
    },
  },
};

const approvedTask: TaskResponse = {
  ...waitingTask,
  status: 'succeeded',
  result: 'Moved "source.txt" to "moved.txt".',
  execution: {
    ...waitingTask.execution!,
    status: 'succeeded',
    result: {
      source: 'source.txt',
      destination: 'moved.txt',
      summary: 'Moved "source.txt" to "moved.txt".',
    },
    completed_at: '2026-09-11T15:00:02Z',
    tool_calls: [
      {
        ...waitingTask.execution!.tool_calls[0],
        status: 'succeeded',
        result: {
          source: 'source.txt',
          destination: 'moved.txt',
          summary: 'Moved "source.txt" to "moved.txt".',
        },
        completed_at: '2026-09-11T15:00:02Z',
      },
    ],
    approval: {
      ...waitingTask.execution!.approval!,
      decision: 'approved',
      decided_at: '2026-09-11T15:00:01Z',
    },
  },
};

const rejectedTask: TaskResponse = {
  ...waitingTask,
  status: 'rejected',
  execution: {
    ...waitingTask.execution!,
    status: 'rejected',
    tool_calls: [
      {
        ...waitingTask.execution!.tool_calls[0],
        status: 'rejected',
      },
    ],
    approval: {
      ...waitingTask.execution!.approval!,
      decision: 'rejected',
      decided_at: '2026-09-11T15:00:01Z',
    },
  },
};

const failedPlanningTask: TaskResponse = {
  ...completedTask,
  status: 'failed',
  result: null,
  error: 'The planner could not create a plan.',
  plan: null,
  execution: null,
};

const failedToolTask: TaskResponse = {
  ...completedTask,
  status: 'failed',
  result: null,
  error: 'Tool "workspace_list" could not be executed safely.',
  execution: {
    ...completedTask.execution!,
    status: 'failed',
    result: null,
    error: 'Tool "workspace_list" could not be executed safely.',
    tool_calls: [
      {
        ...completedTask.execution!.tool_calls[0],
        status: 'failed',
        result: null,
        error: 'Tool "workspace_list" could not be executed safely.',
      },
    ],
  },
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe('App', () => {
  it('renders the Phase 3 task form and approval boundary', () => {
    const markup = renderToStaticMarkup(<App />);

    expect(markup).toContain('Bounded agent workflow');
    expect(markup).toContain('Task instruction');
    expect(markup).toContain('explicit write approval');
  });

  it('renders persisted task, plan, tool call, and result details', () => {
    const markup = renderToStaticMarkup(<TaskResult task={completedTask} />);

    expect(markup).toContain('Task completed');
    expect(markup).toContain('Plan created');
    expect(markup).toContain('Read permitted');
    expect(markup).toContain('Tool completed');
    expect(markup).toContain('workspace_list');
    expect(markup).toContain('README.md');
  });

  it('renders failed planning without claiming plan or tool completion', () => {
    const markup = renderToStaticMarkup(
      <TaskResult task={failedPlanningTask} />,
    );

    expect(markup).toContain('Planning failed');
    expect(markup).toContain('Tool not started');
    expect(markup).not.toContain('Plan created');
    expect(markup).not.toContain('Tool completed');
  });

  it('renders a persisted plan and failed tool without claiming tool completion', () => {
    const markup = renderToStaticMarkup(<TaskResult task={failedToolTask} />);

    expect(markup).toContain('Plan created');
    expect(markup).toContain('Tool failed');
    expect(markup).not.toContain('Tool completed');
  });

  it('shows persisted arguments and decision controls only while waiting', () => {
    const onDecision = vi.fn();
    const waitingMarkup = renderToStaticMarkup(
      <TaskResult task={waitingTask} onDecision={onDecision} />,
    );
    const rejectedMarkup = renderToStaticMarkup(
      <TaskResult task={rejectedTask} onDecision={onDecision} />,
    );

    expect(waitingMarkup).toContain('Waiting for approval');
    expect(waitingMarkup).toContain('workspace_move');
    expect(waitingMarkup).toContain('source.txt');
    expect(waitingMarkup).toContain('Approve');
    expect(waitingMarkup).toContain('Reject');
    expect(rejectedMarkup).toContain('Task rejected');
    expect(rejectedMarkup).toContain('Tool rejected');
    expect(rejectedMarkup).not.toContain('approval-controls');
  });

  it('models submitting, completed, and request failure UI states', () => {
    const submitting = taskExecutionReducer(initialTaskExecutionState, {
      type: 'submit',
    });
    const completed = taskExecutionReducer(submitting, {
      type: 'complete',
      task: completedTask,
    });
    const failed = taskExecutionReducer(submitting, {
      type: 'fail',
      message: 'offline',
    });

    expect(submitting).toEqual({ phase: 'submitting' });
    expect(completed).toEqual({ phase: 'completed', task: completedTask });
    expect(failed).toEqual({ phase: 'request-failed', message: 'offline' });
  });

  it('posts a task instruction to the API', async () => {
    const request = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(completedTask), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const response = await createTask('List this workspace.', request);

    expect(response.status).toBe('succeeded');
    expect(request).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/tasks$/),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ instruction: 'List this workspace.' }),
      }),
    );
  });

  it('posts payload-free approval and rejection decisions', async () => {
    const request = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(approvedTask), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(rejectedTask), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );

    await approveTask(waitingTask.id, request);
    await rejectTask(waitingTask.id, request);

    expect(request).toHaveBeenNthCalledWith(
      1,
      expect.stringMatching(/\/tasks\/.+\/approve$/),
      { method: 'POST' },
    );
    expect(request).toHaveBeenNthCalledWith(
      2,
      expect.stringMatching(/\/tasks\/.+\/reject$/),
      { method: 'POST' },
    );
  });

  it('disables both decisions while approval is processing and submits once', async () => {
    let resolveApproval!: (response: Response) => void;
    const approvalResponse = new Promise<Response>((resolve) => {
      resolveApproval = resolve;
    });
    const request = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(waitingTask), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
      .mockReturnValueOnce(approvalResponse);
    vi.stubGlobal('fetch', request);
    Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
    const container = document.createElement('div');
    document.body.append(container);
    const root = createRoot(container);

    await act(async () => root.render(<App />));
    await act(async () => {
      container
        .querySelector('form')!
        .dispatchEvent(
          new Event('submit', { bubbles: true, cancelable: true }),
        );
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    const approveButton = Array.from(container.querySelectorAll('button')).find(
      (button) => button.textContent === 'Approve',
    )!;

    await act(async () => {
      approveButton.click();
      await Promise.resolve();
    });
    const decisionButtons = container.querySelectorAll<HTMLButtonElement>(
      '.approval-controls button',
    );
    expect(Array.from(decisionButtons).every((button) => button.disabled)).toBe(
      true,
    );
    approveButton.click();
    expect(request).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolveApproval(
        new Response(JSON.stringify(approvedTask), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
      await approvalResponse;
    });
    expect(container.textContent).toContain('Task completed');
    await act(async () => root.unmount());
  });

  it('submits through the component and renders the persisted response', async () => {
    const request = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(completedTask), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', request);
    Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
    const container = document.createElement('div');
    document.body.append(container);
    const root = createRoot(container);

    await act(async () => root.render(<App />));
    const form = container.querySelector('form');
    expect(form).not.toBeNull();

    await act(async () => {
      form!.dispatchEvent(
        new Event('submit', { bubbles: true, cancelable: true }),
      );
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(request).toHaveBeenCalledOnce();
    expect(container.textContent).toContain('Task completed');
    expect(container.textContent).toContain('README.md');
    await act(async () => root.unmount());
  });
});
