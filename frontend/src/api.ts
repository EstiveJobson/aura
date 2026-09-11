export type TaskStatus =
  'pending' | 'planning' | 'executing' | 'succeeded' | 'failed';

export interface WorkspaceEntry {
  name: string;
  kind: 'directory' | 'file';
}

export interface ToolResult {
  workspace: string;
  entries: WorkspaceEntry[];
  entry_count: number;
  summary: string;
}

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
    status: 'running' | 'succeeded' | 'failed';
    result: ToolResult | null;
    error: string | null;
    started_at: string;
    completed_at: string | null;
    tool_calls: Array<{
      id: string;
      tool_name: string;
      arguments: Record<string, unknown>;
      status: 'running' | 'succeeded' | 'failed';
      result: ToolResult | null;
      error: string | null;
      started_at: string;
      completed_at: string | null;
    }>;
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
