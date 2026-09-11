import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { App } from '../src/App';

describe('App', () => {
  it('renders the Phase 0 boundary and next milestone', () => {
    const markup = renderToStaticMarkup(<App />);

    expect(markup).toContain('Foundation online');
    expect(markup).toContain('Ready for Phase 1');
    expect(markup).toContain('Agent execution and task workflows');
  });
});
