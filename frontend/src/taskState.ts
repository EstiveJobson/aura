import type { TaskResponse } from './api';

export type TaskExecutionState =
  | { phase: 'idle' }
  | { phase: 'submitting' }
  | { phase: 'completed'; task: TaskResponse }
  | { phase: 'request-failed'; message: string };

export type TaskExecutionAction =
  | { type: 'submit' }
  | { type: 'complete'; task: TaskResponse }
  | { type: 'fail'; message: string };

export const initialTaskExecutionState: TaskExecutionState = { phase: 'idle' };

export function taskExecutionReducer(
  _state: TaskExecutionState,
  action: TaskExecutionAction,
): TaskExecutionState {
  switch (action.type) {
    case 'submit':
      return { phase: 'submitting' };
    case 'complete':
      return { phase: 'completed', task: action.task };
    case 'fail':
      return { phase: 'request-failed', message: action.message };
  }
}
