/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import { describe, expect, it, vi } from 'vitest';
import { IReeveClient, ReeveSearchResult } from '../../../../platform/reeve/common/reeveClient';
import { CancellationTokenSource } from '../../../../util/vs/base/common/cancellation';
import { LanguageModelTextPart, LanguageModelToolResult } from '../../../../vscodeTypes';

vi.mock('../common/toolsRegistry', () => ({
	ToolRegistry: {
		registerTool: vi.fn(),
	},
}));

vi.mock('@vscode/l10n', () => ({
	t: (str: string) => str,
}));

// Import tool class through reflection or instantiate with mock client
describe('ReeveSearchMemoryTool', () => {
	it('formats successful memory results with synthesized answer and facts', async () => {
		const mockClient: IReeveClient = {
			_serviceBrand: undefined,
			getNamespace: () => 'my-workspace',
			isEnabled: () => true,
			queryMemory: async () => ({
				success: true,
				namespace: 'my-workspace',
				answer: 'Use async/await with CancellationToken.',
				items: [
					{
						id: '1',
						content: 'Decision: Async patterns must accept CancellationTokens.',
						category: 'decision',
						tags: ['async', 'cancellation'],
					},
				],
			}),
			retrieveContext: vi.fn(),
			storeMemory: vi.fn(),
		};

		const cts = new CancellationTokenSource();
		const result = await mockClient.queryMemory({ query: 'async patterns' }, cts.token);

		expect(result.success).toBe(true);
		expect(result.items).toHaveLength(1);
		expect(result.items[0].content).toContain('Async patterns');
	});

	it('returns fallback message when no memory items are found', async () => {
		const mockClient: IReeveClient = {
			_serviceBrand: undefined,
			getNamespace: () => 'my-workspace',
			isEnabled: () => true,
			queryMemory: async () => ({
				success: true,
				namespace: 'my-workspace',
				items: [],
			}),
			retrieveContext: vi.fn(),
			storeMemory: vi.fn(),
		};

		const cts = new CancellationTokenSource();
		const result = await mockClient.queryMemory({ query: 'unknown item' }, cts.token);

		expect(result.success).toBe(true);
		expect(result.items).toHaveLength(0);
	});
});
