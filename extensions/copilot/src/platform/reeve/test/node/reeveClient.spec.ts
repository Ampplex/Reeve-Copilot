/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { mockConfigStore } = vi.hoisted(() => ({
	mockConfigStore: {
		enabled: true,
		endpoint: 'https://mcp.reeve.co.in',
		apiKey: 'test-reeve-api-key',
		namespace: 'test-repo',
		timeoutMs: 50,
	},
}));

vi.mock('vscode', () => {
	return {
		workspace: {
			getConfiguration: (_section: string) => ({
				get: (key: string, defaultValue?: unknown) => {
					if (key in mockConfigStore) {
						return (mockConfigStore as any)[key];
					}
					return defaultValue;
				},
			}),
			workspaceFolders: [
				{ name: 'test-workspace', uri: { fsPath: '/test/workspace' } },
			],
		},
	};
});

import { TestLogService } from '../../../testing/common/testLogService';
import { ReeveClient } from '../../node/reeveClient';

describe('ReeveClient', () => {
	let client: ReeveClient;
	let logService: TestLogService;
	const originalFetch = globalThis.fetch;

	beforeEach(() => {
		mockConfigStore.enabled = true;
		mockConfigStore.endpoint = 'https://mcp.reeve.co.in';
		mockConfigStore.apiKey = 'test-reeve-api-key';
		mockConfigStore.namespace = 'test-repo';
		mockConfigStore.timeoutMs = 50;

		logService = new TestLogService();
		client = new ReeveClient(logService);
	});

	afterEach(() => {
		globalThis.fetch = originalFetch;
		vi.restoreAllMocks();
	});

	it('1. successful search: returns structured memory items and answer', async () => {
		const mockResponseData = {
			success: true,
			answer: 'SQLite is used for local caching with WAL mode enabled.',
			items: [
				{
					id: 'fact-1',
					content: 'Architecture decision: Use SQLite WAL mode for fast concurrency.',
					category: 'architecture',
					timestamp: '2026-01-15T10:00:00Z',
					tags: ['sqlite', 'cache'],
				},
				{
					id: 'fact-2',
					content: 'Constraint: All Reeve memory calls must be non-blocking and fail-safe.',
					category: 'constraint',
					timestamp: '2026-02-01T12:00:00Z',
				},
			],
		};

		globalThis.fetch = vi.fn().mockResolvedValue({
			ok: true,
			status: 200,
			json: async () => mockResponseData,
		} as Response);

		const result = await client.queryMemory({
			query: 'How is local cache configured?',
			category: 'architecture',
			limit: 5,
		});

		expect(result.success).toBe(true);
		expect(result.namespace).toBe('test-repo');
		expect(result.answer).toBe('SQLite is used for local caching with WAL mode enabled.');
		expect(result.items).toHaveLength(2);
		expect(result.items[0].id).toBe('fact-1');
		expect(result.items[0].category).toBe('architecture');
		expect(result.items[0].content).toContain('SQLite WAL mode');
		expect(result.items[1].category).toBe('constraint');
	});

	it('2. empty results: handles empty search results gracefully', async () => {
		globalThis.fetch = vi.fn().mockResolvedValue({
			ok: true,
			status: 200,
			json: async () => ({ items: [] }),
		} as Response);

		const result = await client.queryMemory({
			query: 'Nonexistent decision',
		});

		expect(result.success).toBe(true);
		expect(result.items).toHaveLength(0);
		expect(result.error).toBeUndefined();
	});

	it('3. timeout: aborts within timeout period and returns fail-safe error', async () => {
		// Mock fetch that hangs forever until aborted
		globalThis.fetch = vi.fn().mockImplementation((_url, init) => {
			return new Promise((_, reject) => {
				const signal = init?.signal as AbortSignal;
				if (signal) {
					signal.addEventListener('abort', () => {
						const err = new Error('The operation was aborted');
						err.name = 'AbortError';
						reject(err);
					});
				}
			});
		});

		mockConfigStore.timeoutMs = 25; // 25ms timeout
		const result = await client.queryMemory({
			query: 'Query that will time out',
		});

		expect(result.success).toBe(false);
		expect(result.items).toHaveLength(0);
		expect(result.error).toMatch(/timed out/i);
	});

	it('4. connection failure: handles network / HTTP error without throwing', async () => {
		globalThis.fetch = vi.fn().mockRejectedValue(new Error('ECONNREFUSED: connect failure'));

		const result = await client.queryMemory({
			query: 'Network test',
		});

		expect(result.success).toBe(false);
		expect(result.items).toHaveLength(0);
		expect(result.error).toContain('ECONNREFUSED');
	});

	it('5. disabled Reeve: skips network request when disabled by configuration', async () => {
		mockConfigStore.enabled = false;
		const fetchMock = vi.fn();
		globalThis.fetch = fetchMock;

		expect(client.isEnabled()).toBe(false);

		const result = await client.queryMemory({
			query: 'Should not fetch',
		});

		expect(result.success).toBe(false);
		expect(result.items).toHaveLength(0);
		expect(result.error).toContain('disabled');
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('6. storeMemory: stores durable memory items successfully', async () => {
		globalThis.fetch = vi.fn().mockResolvedValue({
			ok: true,
			status: 200,
			json: async () => ({ success: true, id: 'stored-123' }),
		} as Response);

		const storeResult = await client.storeMemory?.({
			fact: 'Refactored auth module to use PKCE',
			speaker: 'agent',
			category: 'architecture',
		});

		expect(storeResult?.success).toBe(true);
		expect(storeResult?.id).toBe('stored-123');
	});
});
