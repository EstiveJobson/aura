export type TaskStatus =
  | 'pending'
  | 'planning'
  | 'waiting_for_approval'
  | 'executing'
  | 'succeeded'
  | 'rejected'
  | 'failed'
  | 'outcome_uncertain';

export interface WorkspaceEntry {
  name: string;
  kind: 'directory' | 'file';
}

export type ToolResult = Record<string, unknown>;

export interface PlannedStep {
  sequence: number;
  title: string;
  tool_name: string;
  arguments: Record<string, unknown>;
}

export interface TaskResponse {
  id: string;
  instruction: string;
  status: TaskStatus;
  result: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  plan: {
    id: string;
    planner: string;
    summary: string;
    steps: PlannedStep[];
    created_at: string;
  } | null;
  execution: {
    id: string;
    status:
      | 'waiting_for_approval'
      | 'running'
      | 'succeeded'
      | 'rejected'
      | 'failed'
      | 'outcome_uncertain';
    result: ToolResult | null;
    error: string | null;
    started_at: string;
    completed_at: string | null;
    tool_calls: Array<{
      id: string;
      tool_name: string;
      arguments: Record<string, unknown>;
      status:
        | 'waiting_for_approval'
        | 'running'
        | 'succeeded'
        | 'rejected'
        | 'failed'
        | 'outcome_uncertain';
      result: ToolResult | null;
      error: string | null;
      started_at: string;
      completed_at: string | null;
    }>;
    approval: {
      id: string;
      decision: 'pending' | 'approved' | 'rejected';
      requested_at: string;
      decided_at: string | null;
    } | null;
  } | null;
}

const apiBaseUrl =
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000/api';

export async function createTask(
  instruction: string,
  request: typeof fetch = fetch,
): Promise<TaskResponse> {
  const response = await request(`${apiBaseUrl}/tasks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ instruction }),
  });

  if (!response.ok) {
    throw new Error(`Task request failed with status ${response.status}.`);
  }

  return (await response.json()) as TaskResponse;
}

async function decideTask(
  taskId: string,
  decision: 'approve' | 'reject',
  request: typeof fetch,
): Promise<TaskResponse> {
  const response = await request(`${apiBaseUrl}/tasks/${taskId}/${decision}`, {
    method: 'POST',
    headers: { 'X-AURA-Decision': decision },
  });

  if (!response.ok) {
    throw new Error(
      `Task ${decision} request failed with status ${response.status}.`,
    );
  }
  return (await response.json()) as TaskResponse;
}

export function approveTask(
  taskId: string,
  request: typeof fetch = fetch,
): Promise<TaskResponse> {
  return decideTask(taskId, 'approve', request);
}

export function rejectTask(
  taskId: string,
  request: typeof fetch = fetch,
): Promise<TaskResponse> {
  return decideTask(taskId, 'reject', request);
}
