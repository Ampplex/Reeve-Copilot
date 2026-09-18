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

	it('7. native MCP over SSE: performs handshake, passes Bearer auth, and executes retrieve_memory_context tool', async () => {
		let streamController: any;
		const encoder = new TextEncoder();
		const capturedCalls: Array<{ url: string; method: string; headers: Record<string, string>; body?: any }> = [];

		globalThis.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
			const headers = (init?.headers ?? {}) as Record<string, string>;
			capturedCalls.push({
				url: String(url),
				method: init?.method ?? 'GET',
				headers,
				body: init?.body ? JSON.parse(String(init.body)) : undefined,
			});

			if (String(url).endsWith('/sse')) {
				const stream = new ReadableStream<Uint8Array>({
					start(controller) {
						streamController = controller;
						controller.enqueue(encoder.encode('event: endpoint\ndata: /messages?session_id=sess-abc\n\n'));
					},
				});
				return {
					ok: true,
					status: 200,
					headers: new Headers({ 'content-type': 'text/event-stream' }),
					body: stream,
				} as Response;
			}

			if (String(url).includes('/messages?session_id=sess-abc')) {
				const body = JSON.parse(String(init?.body));
				if (body.method === 'initialize') {
					streamController.enqueue(encoder.encode(`event: message\ndata: ${JSON.stringify({ jsonrpc: '2.0', id: body.id, result: { serverInfo: { name: 'threelane-memory' } } })}\n\n`));
					return { ok: true, status: 200 } as Response;
				}
				if (body.method === 'notifications/initialized') {
					return { ok: true, status: 200 } as Response;
				}
				if (body.method === 'tools/call' && body.params.name === 'retrieve_memory_context') {
					streamController.enqueue(encoder.encode(`event: message\ndata: ${JSON.stringify({
						jsonrpc: '2.0',
						id: body.id,
						result: {
							content: [{
								type: 'text',
								text: 'Relevant Reeve Memory:\n- [Episode 1] Fact: We use SQLite WAL mode for concurrency.\n- [Episode 2] Fact: Memory calls must be non-blocking.'
							}]
						}
					})}\n\n`));
					return { ok: true, status: 200 } as Response;
				}
			}

			return { ok: false, status: 404 } as Response;
		});

		const result = await client.queryMemory({
			query: 'How is concurrency handled?',
		});

		expect(result.success).toBe(true);
		expect(result.items.length).toBe(2);
		expect(result.items[0].content).toContain('SQLite WAL mode');
		expect(result.items[1].content).toContain('non-blocking');

		// Verify Bearer auth token was passed on SSE GET and message POST
		expect(capturedCalls[0].url).toContain('/sse');
		expect(capturedCalls[0].headers['Authorization']).toBe('Bearer test-reeve-api-key');
		expect(capturedCalls[1].headers['Authorization']).toBe('Bearer test-reeve-api-key');
	});

	it('8. native MCP over SSE: executes store_memory tool with speaker partition', async () => {
		let streamController: any;
		const encoder = new TextEncoder();
		const capturedCalls: Array<{ url: string; method: string; headers: Record<string, string>; body?: any }> = [];

		globalThis.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
			const headers = (init?.headers ?? {}) as Record<string, string>;
			capturedCalls.push({
				url: String(url),
				method: init?.method ?? 'GET',
				headers,
				body: init?.body ? JSON.parse(String(init.body)) : undefined,
			});

			if (String(url).endsWith('/sse')) {
				const stream = new ReadableStream<Uint8Array>({
					start(controller) {
						streamController = controller;
						controller.enqueue(encoder.encode('event: endpoint\ndata: /messages?session_id=sess-store\n\n'));
					},
				});
				return {
					ok: true,
					status: 200,
					headers: new Headers({ 'content-type': 'text/event-stream' }),
					body: stream,
				} as Response;
			}

			if (String(url).includes('/messages?session_id=sess-store')) {
				const body = JSON.parse(String(init?.body));
				if (body.method === 'initialize') {
					streamController.enqueue(encoder.encode(`event: message\ndata: ${JSON.stringify({ jsonrpc: '2.0', id: body.id, result: { serverInfo: { name: 'threelane-memory' } } })}\n\n`));
					return { ok: true, status: 200 } as Response;
				}
				if (body.method === 'notifications/initialized') {
					return { ok: true, status: 200 } as Response;
				}
				if (body.method === 'tools/call' && body.params.name === 'store_memory') {
					// Verify tool arguments match Reeve schema (text, speaker)
					expect(body.params.arguments.text).toBe('User prefers Python over JavaScript');
					expect(body.params.arguments.speaker).toBe('test-repo');

					streamController.enqueue(encoder.encode(`event: message\ndata: ${JSON.stringify({
						jsonrpc: '2.0',
						id: body.id,
						result: {
							content: [{
								type: 'text',
								text: JSON.stringify({ pending_id: 'pending-abc-123', persisting: true })
							}]
						}
					})}\n\n`));
					return { ok: true, status: 200 } as Response;
				}
			}

			return { ok: false, status: 404 } as Response;
		});

		const storeResult = await client.storeMemory?.({
			fact: 'User prefers Python over JavaScript',
			namespace: 'test-repo',
		});

		expect(storeResult?.success).toBe(true);
		expect(storeResult?.id).toBe('pending-abc-123');

		// Verify Bearer auth header on store call
		const storeCall = capturedCalls.find(c => c.body?.method === 'tools/call');
		expect(storeCall?.headers['Authorization']).toBe('Bearer test-reeve-api-key');
	});

	it('9. authentication error (HTTP 401): returns clear auth failure without throwing', async () => {
		globalThis.fetch = vi.fn().mockResolvedValue({
			ok: false,
			status: 401,
			statusText: 'Unauthorized',
			headers: new Headers({ 'www-authenticate': 'Bearer realm="mcp"' }),
		} as Response);

		const result = await client.queryMemory({
			query: 'Should fail with 401',
		});

		expect(result.success).toBe(false);
		expect(result.error).toMatch(/401/);
		expect(result.error).toMatch(/Authentication failed|API error/i);
	});
});
