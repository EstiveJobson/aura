import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { App, TaskResult } from '../src/App';
import { createTask } from '../src/api';
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
  },
};

describe('App', () => {
  it('renders the Phase 1 task form and read-only boundary', () => {
    const markup = renderToStaticMarkup(<App />);

    expect(markup).toContain('First agent workflow');
    expect(markup).toContain('Task instruction');
    expect(markup).toContain('workspace_list · read only');
  });

  it('renders persisted task, plan, tool call, and result details', () => {
    const markup = renderToStaticMarkup(<TaskResult task={completedTask} />);

    expect(markup).toContain('Task completed');
    expect(markup).toContain('Plan created');
    expect(markup).toContain('workspace_list');
    expect(markup).toContain('README.md');
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
});
