/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import * as cp from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { ILogService } from '../../log/common/logService';
import { CancellationToken } from '../../../util/vs/base/common/cancellation';
import {
	IReeveClient,
	ReeveMemoryItem,
	ReeveSearchParams,
	ReeveSearchResult,
	ReeveStoreParams,
	ReeveStoreResult
} from '../common/reeveClient';
import { ISessionActionObserver, ActionExplanation, ReeveActionEvent } from '../common/reeveActionObserver';
import { HumanCenteredExplanationLayer } from './humanCenteredExplanationLayer';

const DEFAULT_ENDPOINT = 'https://mcp.reeve.co.in';
const DEFAULT_TIMEOUT_MS = 15000;
const CONFIG_SECTION = 'github.copilot.reeve';

/**
 * Manages an active streaming connection with the Reeve Memory Engine over Server-Sent Events (SSE).
 * Connects to Reeve at `${endpoint}/sse`, establishes protocol session,
 * and executes memory operations (e.g. store_memory, retrieve_memory_context, query_memory)
 * with Bearer authentication on every request.
 */
class ReeveProtocolConnection {
	private messageUrl: string | null = null;
	private abortController: AbortController | null = null;
	private pendingRequests = new Map<string | number, { resolve: (val: any) => void; reject: (err: any) => void }>();
	private nextId = 1;
	private readyPromise: Promise<string> | null = null;
	private resolveReady: ((url: string) => void) | null = null;
	private rejectReady: ((err: any) => void) | null = null;

	private connectingPromise: Promise<string> | null = null;

	constructor(
		private readonly baseEndpoint: string,
		private readonly getAuthToken: () => string | undefined,
		private readonly logService: ILogService
	) { }

	public isConnected(): boolean {
		return !!this.messageUrl && !this.abortController?.signal.aborted;
	}

	public close(): void {
		this.connectingPromise = null;
		if (this.abortController) {
			try {
				this.abortController.abort();
			} catch {
				// ignore
			}
			this.abortController = null;
		}
		this.messageUrl = null;
		for (const [, pending] of this.pendingRequests) {
			pending.reject(new Error('Connection closed'));
		}
		this.pendingRequests.clear();
	}

	public async ensureConnected(timeoutMs: number, token?: CancellationToken): Promise<string> {
		if (this.isConnected() && this.messageUrl) {
			return this.messageUrl;
		}

		if (this.connectingPromise) {
			return this.connectingPromise;
		}

		this.close();
		const abortController = new AbortController();
		this.abortController = abortController;

		const sseUrl = this.baseEndpoint.endsWith('/sse')
			? this.baseEndpoint
			: `${this.baseEndpoint.replace(/\/+$/, '')}/sse`;

		const authToken = this.getAuthToken();
		const headers: Record<string, string> = {
			'Accept': 'text/event-stream',
			'Cache-Control': 'no-cache',
		};
		if (authToken) {
			headers['Authorization'] = `Bearer ${authToken}`;
		}

		let endpointTimer: NodeJS.Timeout | undefined;
		const timeoutPromise = new Promise<never>((_, reject) => {
			endpointTimer = setTimeout(() => {
				abortController.abort(new Error(`Timed out connecting to Reeve SSE after ${timeoutMs}ms`));
				reject(new Error(`Timed out connecting to Reeve SSE after ${timeoutMs}ms`));
			}, timeoutMs);
		});

		const cancelPromise = new Promise<never>((_, reject) => {
			token?.onCancellationRequested(() => {
				abortController.abort(new Error('Operation cancelled'));
				reject(new Error('Operation cancelled'));
			});
		});

		const connectPromise = (async () => {
			this.logService.debug(`[ReeveClient] Connecting to Reeve SSE: ${sseUrl} (Auth: ${authToken ? 'present' : 'none'})`);
			const res = await fetch(sseUrl, {
				method: 'GET',
				headers,
				signal: abortController.signal,
			});

			if (!res.ok) {
				const authErr = res.status === 401 ? ' (Authentication failed: invalid or missing Bearer token)' : '';
				throw new Error(`HTTP ${res.status}: ${res.statusText}${authErr}`);
			}

			const contentType = res.headers?.get('content-type') || '';
			// If the server or test mock returns direct JSON rather than SSE, trigger fallback
			if (!contentType.includes('text/event-stream') && !res.body) {
				throw new Error('DIRECT_HTTP_FALLBACK');
			}

			const messageUrlPromise = new Promise<string>((resolve, reject) => {
				this.resolveReady = resolve;
				this.rejectReady = reject;
			});

			// Start background SSE event reader
			this.startReader(res, this.baseEndpoint);

			// Wait for endpoint event from SSE stream
			const messageUrl = await messageUrlPromise;

			// Perform MCP initialize handshake
			await this.handshake(messageUrl, authToken, timeoutMs, abortController.signal);
			this.messageUrl = messageUrl;
			return messageUrl;
		})();

		this.connectingPromise = (async () => {
			try {
				return await Promise.race([connectPromise, timeoutPromise, cancelPromise]);
			} catch (err) {
				this.close();
				throw err;
			} finally {
				this.connectingPromise = null;
				if (endpointTimer) {
					clearTimeout(endpointTimer);
				}
			}
		})();

		return await this.connectingPromise;
	}

	private async startReader(res: Response, baseEndpoint: string): Promise<void> {
		if (!res.body) {
			this.rejectReady?.(new Error('No response body for SSE stream'));
			return;
		}

		const reader = (res.body as any).getReader();
		const decoder = new TextDecoder();
		let buffer = '';
		let currentEvent = 'message';
		let currentData = '';

		try {
			while (true) {
				const { done, value } = await reader.read();
				if (done) {
					break;
				}

				buffer += decoder.decode(value, { stream: true });
				const lines = buffer.split(/\r?\n/);
				buffer = lines.pop() ?? '';

				for (const line of lines) {
					if (line.trim() === '') {
						// Completed SSE event block
						if (currentEvent === 'endpoint' && currentData) {
							const targetUrl = new URL(currentData.trim(), baseEndpoint).toString();
							this.resolveReady?.(targetUrl);
						} else if (currentData) {
							try {
								const payload = JSON.parse(currentData);
								if (payload && payload.id !== undefined) {
									const pending = this.pendingRequests.get(payload.id)
										?? this.pendingRequests.get(Number(payload.id))
										?? this.pendingRequests.get(String(payload.id));
									if (pending) {
										this.pendingRequests.delete(payload.id);
										this.pendingRequests.delete(Number(payload.id));
										this.pendingRequests.delete(String(payload.id));
										if (payload.error) {
											pending.reject(new Error(payload.error.message || JSON.stringify(payload.error)));
										} else {
											pending.resolve(payload.result);
										}
									}
								}
							} catch {
								// ignore non-JSON message chunks
							}
						}
						currentEvent = 'message';
						currentData = '';
						continue;
					}

					if (line.startsWith('event:')) {
						currentEvent = line.slice(6).trim();
					} else if (line.startsWith('data:')) {
						const chunk = line.slice(5).trim();
						currentData = currentData ? `${currentData}\n${chunk}` : chunk;
					}
				}
			}
		} catch {
			// Stream ended or aborted
		} finally {
			this.close();
		}
	}

	private async handshake(messageUrl: string, authToken: string | undefined, timeoutMs: number, signal?: AbortSignal): Promise<void> {
		const initId = `init-${Date.now()}`;
		const headers: Record<string, string> = {
			'Content-Type': 'application/json',
		};
		if (authToken) {
			headers['Authorization'] = `Bearer ${authToken}`;
		}

		const initPromise = new Promise((resolve, reject) => {
			const timer = setTimeout(() => {
				this.pendingRequests.delete(initId);
				reject(new Error(`Reeve session handshake timed out after ${timeoutMs}ms`));
			}, timeoutMs);

			this.pendingRequests.set(initId, {
				resolve: (val) => {
					clearTimeout(timer);
					resolve(val);
				},
				reject: (err) => {
					clearTimeout(timer);
					reject(err);
				},
			});
		});

		// 1. Send initialize request
		await fetch(messageUrl, {
			method: 'POST',
			headers,
			signal,
			body: JSON.stringify({
				jsonrpc: '2.0',
				id: initId,
				method: 'initialize',
				params: {
					protocolVersion: '2024-11-05',
					capabilities: {},
					clientInfo: {
						name: 'reeve-copilot',
						version: '1.0.0',
					},
				},
			}),
		});

		await initPromise;

		// 2. Send notifications/initialized notification
		await fetch(messageUrl, {
			method: 'POST',
			headers,
			signal,
			body: JSON.stringify({
				jsonrpc: '2.0',
				method: 'notifications/initialized',
				params: {},
			}),
		});
	}

	public async callTool(name: string, args: Record<string, any>, timeoutMs: number, token?: CancellationToken): Promise<any> {
		const messageUrl = await this.ensureConnected(timeoutMs, token);
		const reqId = this.nextId++;
		const authToken = this.getAuthToken();

		const headers: Record<string, string> = {
			'Content-Type': 'application/json',
		};
		if (authToken) {
			headers['Authorization'] = `Bearer ${authToken}`;
		}

		return new Promise(async (resolve, reject) => {
			const timer = setTimeout(() => {
				this.pendingRequests.delete(reqId);
				reject(new Error(`Tool call '${name}' timed out after ${timeoutMs}ms`));
			}, timeoutMs);

			const cancelListener = token?.onCancellationRequested(() => {
				this.pendingRequests.delete(reqId);
				clearTimeout(timer);
				reject(new Error('Operation cancelled'));
			});

			this.pendingRequests.set(reqId, {
				resolve: (result) => {
					clearTimeout(timer);
					cancelListener?.dispose();
					resolve(result);
				},
				reject: (err) => {
					clearTimeout(timer);
					cancelListener?.dispose();
					reject(err);
				},
			});

			try {
				const res = await fetch(messageUrl, {
					method: 'POST',
					headers,
					body: JSON.stringify({
						jsonrpc: '2.0',
						id: reqId,
						method: 'tools/call',
						params: {
							name,
							arguments: args,
						},
					}),
				});

				if (!res.ok) {
					this.pendingRequests.delete(reqId);
					clearTimeout(timer);
					cancelListener?.dispose();
					if (res.status === 404) {
						// Stale session, tear down so next call re-establishes session
						this.close();
					}
					reject(new Error(`HTTP ${res.status}: ${res.statusText}`));
				}
			} catch (err) {
				this.pendingRequests.delete(reqId);
				clearTimeout(timer);
				cancelListener?.dispose();
				reject(err);
			}
		});
	}
}

async function extractAttachmentText(references: readonly any[] | undefined): Promise<string> {
	if (!references || references.length === 0) {
		return '';
	}
	const chunks: string[] = [];
	for (const ref of references) {
		try {
			let fileUri: vscode.Uri | undefined;
			if (ref.value instanceof vscode.Uri) {
				fileUri = ref.value;
			} else if (ref.value && typeof ref.value === 'object' && 'uri' in ref.value && (ref.value as any).uri instanceof vscode.Uri) {
				fileUri = (ref.value as any).uri;
			}
			if (!fileUri || fileUri.scheme !== 'file') {
				continue;
			}
			const filePath = fileUri.fsPath;
			if (!fs.existsSync(filePath)) {
				continue;
			}
			const ext = path.extname(filePath).toLowerCase();
			if (ext === '.pdf') {
				try {
					const script = `import pypdf, sys; r = pypdf.PdfReader(sys.argv[1]); print('\\n'.join(p.extract_text() or '' for p in r.pages))`;
					const stdout = cp.execFileSync('python3', ['-c', script, filePath], { encoding: 'utf8', maxBuffer: 10 * 1024 * 1024, timeout: 10000 });
					if (stdout.trim()) {
						chunks.push(`[Attached Document: "${path.basename(filePath)}"]\n${stdout.trim()}`);
					}
				} catch {
					// best-effort PDF extraction
				}
			} else if (['.txt', '.md', '.markdown', '.json', '.js', '.ts', '.py', '.java', '.go', '.rs', '.cpp', '.c', '.h', '.html', '.css', '.yaml', '.yml'].includes(ext)) {
				const content = await fs.promises.readFile(filePath, 'utf8');
				if (content.trim()) {
					chunks.push(`[Attached Document: "${path.basename(filePath)}"]\n${content.trim().slice(0, 50000)}`);
				}
			}
		} catch {
			// ignore reference read errors
		}
	}
	return chunks.join('\n\n');
}

export class ReeveClient implements IReeveClient {
	readonly _serviceBrand: undefined;
	private connection: ReeveProtocolConnection | null = null;
	private cachedEndpoint = '';
	private readonly explanationLayer = new HumanCenteredExplanationLayer();
	private readonly lastRecalledMemories = new Map<string, readonly ReeveMemoryItem[]>();

	constructor(
		@ILogService private readonly logService: ILogService,
	) { }

	public isEnabled(): boolean {
		try {
			const config = vscode.workspace.getConfiguration(CONFIG_SECTION);
			return config.get<boolean>('enabled', true);
		} catch {
			return true;
		}
	}

	public getNamespace(): string {
		try {
			const config = vscode.workspace.getConfiguration(CONFIG_SECTION);
			const explicitNamespace = config.get<string>('namespace', '').trim();
			if (explicitNamespace) {
				return explicitNamespace;
			}

			// Auto-detect from active workspace folder name
			const workspaceFolders = vscode.workspace.workspaceFolders;
			if (workspaceFolders && workspaceFolders.length > 0) {
				return workspaceFolders[0].name;
			}
		} catch {
			// fallback
		}
		return 'default';
	}

	private getEndpoint(): string {
		try {
			const config = vscode.workspace.getConfiguration(CONFIG_SECTION);
			const endpoint = config.get<string>('endpoint', DEFAULT_ENDPOINT)?.trim();
			return endpoint || DEFAULT_ENDPOINT;
		} catch {
			return DEFAULT_ENDPOINT;
		}
	}

	private getApiKey(): string | undefined {
		try {
			const config = vscode.workspace.getConfiguration(CONFIG_SECTION);
			const key = config.get<string>('apiKey', '')?.trim();
			if (key) {
				return key;
			}
			const token = config.get<string>('authToken', '')?.trim();
			if (token) {
				return token;
			}
		} catch {
			// ignore
		}
		return process.env['REEVE_API_KEY'] || process.env['REEVE_AUTH_TOKEN'];
	}

	private getTimeoutMs(): number {
		try {
			const config = vscode.workspace.getConfiguration(CONFIG_SECTION);
			return config.get<number>('timeoutMs', DEFAULT_TIMEOUT_MS) || DEFAULT_TIMEOUT_MS;
		} catch {
			return DEFAULT_TIMEOUT_MS;
		}
	}

	private getOrCreateConnection(): ReeveProtocolConnection {
		const endpoint = this.getEndpoint().replace(/\/+$/, '');
		if (!this.connection || this.cachedEndpoint !== endpoint) {
			this.connection?.close();
			this.cachedEndpoint = endpoint;
			this.connection = new ReeveProtocolConnection(
				endpoint,
				() => this.getApiKey(),
				this.logService
			);
		}
		return this.connection;
	}

	private buildHeaders(): Record<string, string> {
		const headers: Record<string, string> = {
			'Content-Type': 'application/json',
			'Accept': 'application/json',
		};
		const apiKey = this.getApiKey();
		if (apiKey) {
			headers['Authorization'] = `Bearer ${apiKey}`;
		}
		return headers;
	}

	public async queryMemory(params: ReeveSearchParams, token?: CancellationToken): Promise<ReeveSearchResult> {
		const namespace = params.namespace?.trim() || this.getNamespace();

		if (!this.isEnabled()) {
			this.logService.debug('[ReeveClient] Integration disabled by configuration.');
			return {
				success: false,
				items: [],
				namespace,
				error: 'Reeve memory integration is disabled in settings.'
			};
		}

		const timeoutMs = this.getTimeoutMs();
		this.logService.debug(`[ReeveClient] Querying Reeve memory: query="${params.query}", namespace="${namespace}"`);

		// 1. First attempt: Native Reeve memory retrieval over SSE (retrieve_memory_context / query_memory)
		try {
			const connection = this.getOrCreateConnection();
			const result = await connection.callTool(
				'retrieve_memory_context',
				{ question: params.query, speaker: namespace },
				timeoutMs,
				token
			);

			const items: ReeveMemoryItem[] = [];
			let rawContextText = '';

			const content = Array.isArray(result?.content) ? result.content : [];
			for (const block of content) {
				if (block.type === 'text' && typeof block.text === 'string') {
					rawContextText += (rawContextText ? '\n' : '') + block.text;
				}
			}

			if (rawContextText.trim()) {
				// Parse text into memory items (split by lines or bullet points)
				const lines = rawContextText.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
				for (let i = 0; i < lines.length; i++) {
					const line = lines[i];
					if (line.startsWith('#') || (line.startsWith('[') && line.endsWith(']')) || line.toLowerCase().includes('memory:') || line.endsWith(':')) {
						continue; // skip header lines
					}
					const cleanText = line.replace(/^[-*•]\s*/, '');
					if (cleanText) {
						items.push({
							id: `reeve-mem-${i + 1}`,
							content: cleanText,
							category: 'memory',
						});
					}
				}
			}

			this.logService.info(`[ReeveClient] Reeve engine retrieved ${items.length} memory item(s) for "${params.query}"`);
			return {
				success: true,
				items,
				namespace,
				answer: rawContextText || undefined,
			};
		} catch (mcpErr: any) {
			const mcpMessage = mcpErr?.message ?? String(mcpErr);
			this.logService.debug(`[ReeveClient] Reeve tool call failed (${mcpMessage}), trying HTTP fallback.`);

			// If it was cancelled or explicitly timed out, return immediately
			if (mcpMessage.includes('timed out') || mcpMessage.includes('cancelled')) {
				return {
					success: false,
					items: [],
					namespace,
					error: mcpMessage,
				};
			}
		}

		// 2. Fallback: Direct HTTP endpoint (for test mocks or custom REST gateways)
		const endpoint = this.getEndpoint().replace(/\/+$/, '');
		const queryUrl = `${endpoint}/api/v1/memory/query`;

		const abortController = new AbortController();
		const timer = setTimeout(() => {
			abortController.abort(new Error(`Reeve request timed out after ${timeoutMs}ms`));
		}, timeoutMs);

		const cancellationListener = token?.onCancellationRequested(() => {
			abortController.abort(new Error('Operation cancelled'));
		});

		try {
			const response = await fetch(queryUrl, {
				method: 'POST',
				headers: this.buildHeaders(),
				body: JSON.stringify({
					query: params.query,
					namespace,
					speaker: params.speaker,
					category: params.category,
					limit: params.limit ?? 5,
				}),
				signal: abortController.signal,
			});

			if (!response.ok) {
				const authErr = response.status === 401 ? ' (Authentication failed: invalid or missing Bearer token)' : '';
				const errorMsg = `HTTP ${response.status}: ${response.statusText}${authErr}`;
				this.logService.warn(`[ReeveClient] Memory query failed: ${errorMsg}`);
				return {
					success: false,
					items: [],
					namespace,
					error: `Reeve API error: ${errorMsg}`
				};
			}

			const data = await response.json() as any;
			const rawItems: any[] = Array.isArray(data.items)
				? data.items
				: Array.isArray(data.results)
					? data.results
					: Array.isArray(data.memories)
						? data.memories
						: [];

			const items: ReeveMemoryItem[] = rawItems.map((item: any, index: number) => ({
				id: String(item.id ?? `mem-${index + 1}`),
				content: typeof item.content === 'string' ? item.content : (item.text ?? item.fact ?? JSON.stringify(item)),
				category: item.category,
				timestamp: item.timestamp ?? item.created_at,
				validFrom: item.valid_from ?? item.validFrom,
				validTo: item.valid_to ?? item.validTo,
				supersededBy: item.superseded_by ?? item.supersededBy,
				tags: Array.isArray(item.tags) ? item.tags : undefined,
				score: typeof item.score === 'number' ? item.score : undefined,
			}));

			const answer = typeof data.answer === 'string' ? data.answer : undefined;

			this.logService.info(`[ReeveClient] Successfully retrieved ${items.length} memory item(s) for "${params.query}"`);
			return {
				success: true,
				items,
				namespace,
				answer,
			};
		} catch (err: any) {
			const isAbort = err?.name === 'AbortError' || abortController.signal.aborted;
			const message = isAbort
				? `Request timed out after ${timeoutMs}ms or was cancelled`
				: (err?.message ?? String(err));

			this.logService.warn(`[ReeveClient] Could not reach Reeve endpoint: ${message}`);
			return {
				success: false,
				items: [],
				namespace,
				error: message,
			};
		} finally {
			clearTimeout(timer);
			cancellationListener?.dispose();
		}
	}

	public async retrieveContext(topicOrEntity: string, namespace?: string, token?: CancellationToken): Promise<ReeveSearchResult> {
		return this.queryMemory({
			query: topicOrEntity,
			namespace,
			limit: 10,
		}, token);
	}

	public async storeMemory(params: ReeveStoreParams, token?: CancellationToken): Promise<ReeveStoreResult> {
		const namespace = params.namespace?.trim() || this.getNamespace();

		if (!this.isEnabled()) {
			return { success: false, error: 'Reeve integration is disabled in settings.' };
		}

		const timeoutMs = this.getTimeoutMs();

		// 1. First attempt: Native Reeve memory storage over SSE (tool: store_memory)
		try {
			const connection = this.getOrCreateConnection();
			const result = await connection.callTool(
				'store_memory',
				{ text: params.fact, speaker: namespace },
				timeoutMs,
				token
			);

			let pendingId: string | undefined;
			const content = Array.isArray(result?.content) ? result.content : [];
			for (const block of content) {
				if (block.type === 'text' && typeof block.text === 'string') {
					try {
						const parsed = JSON.parse(block.text);
						pendingId = parsed.pending_id || parsed.id || parsed.result_id;
					} catch {
						// not JSON, ignore
					}
				}
			}

			this.logService.info(`[ReeveClient] Stored memory via Reeve SSE successfully (${pendingId || 'persisting'})`);
			return { success: true, id: pendingId };
		} catch (mcpErr: any) {
			const mcpMessage = mcpErr?.message ?? String(mcpErr);
			this.logService.debug(`[ReeveClient] Reeve store_memory call failed (${mcpMessage}), trying HTTP fallback.`);

			if (mcpMessage.includes('timed out') || mcpMessage.includes('cancelled')) {
				return { success: false, error: mcpMessage };
			}
		}

		// 2. Fallback: Direct HTTP endpoint (for test mocks or custom REST gateways)
		const endpoint = this.getEndpoint().replace(/\/+$/, '');
		const storeUrl = `${endpoint}/api/v1/memory/store`;

		const abortController = new AbortController();
		const timer = setTimeout(() => {
			abortController.abort(new Error(`Reeve request timed out after ${timeoutMs}ms`));
		}, timeoutMs);

		const cancellationListener = token?.onCancellationRequested(() => {
			abortController.abort(new Error('Operation cancelled'));
		});

		try {
			const response = await fetch(storeUrl, {
				method: 'POST',
				headers: this.buildHeaders(),
				body: JSON.stringify({
					fact: params.fact,
					namespace,
					speaker: params.speaker,
					category: params.category,
				}),
				signal: abortController.signal,
			});

			if (!response.ok) {
				const authErr = response.status === 401 ? ' (Authentication failed: invalid or missing Bearer token)' : '';
				return { success: false, error: `HTTP ${response.status}: ${response.statusText}${authErr}` };
			}

			const data = await response.json() as any;
			return { success: true, id: data.id ? String(data.id) : undefined };
		} catch (err: any) {
			return { success: false, error: err?.message ?? String(err) };
		} finally {
			clearTimeout(timer);
			cancellationListener?.dispose();
		}
	}

	/**
	 * Recalls relevant project context from Reeve and injects it into the chat request prompt.
	 * Completely encapsulates memory retrieval, reference citation, and directive formatting.
	 */
	public async preparePromptWithMemory(
		request: vscode.ChatRequest,
		stream: vscode.ChatResponseStream,
		token?: CancellationToken
	): Promise<{ request: vscode.ChatRequest; hasMemory: boolean; namespace: string }> {
		const namespace = this.getNamespace();
		if (!this.isEnabled() || !request.prompt) {
			return { request, hasMemory: false, namespace };
		}

		try {
			stream.progress('Recalling Reeve memory...');
			const memoryResult = await this.queryMemory({
				query: request.prompt,
				limit: 5,
			}, token);

			if (memoryResult && memoryResult.success && (memoryResult.items.length > 0 || (memoryResult.answer && memoryResult.answer.trim()))) {
				const sessionId = (request as any).sessionId || 'default_session';
				this.lastRecalledMemories.set(sessionId, memoryResult.items || []);

				const contextEntries = (memoryResult.answer && memoryResult.answer.trim())
					? memoryResult.answer.trim()
					: memoryResult.items.map(item => {
						const cat = item.category ? ` [${item.category}]` : '';
						return `• ${item.content}${cat}`;
					}).join('\n');

				const injectedContext = `[Reeve Long-Term Project Memory (namespace: "${namespace}"):
${contextEntries}

CRITICAL INSTRUCTION: You MUST use the above Reeve Long-Term Project Memory to answer the user request directly. Do NOT attempt to search workspace files or repo codebase when the answer is provided in this memory. Answer naturally and authoritatively as Copilot with Reeve Long-Term Memory.]\n\n`;

				return {
					request: {
						...request,
						prompt: `${injectedContext}${request.prompt}`,
					},
					hasMemory: true,
					namespace,
				};
			}
		} catch (err) {
			this.logService.warn('[ReeveClient] Failed to recall Reeve memory:', err);
		}

		return { request, hasMemory: false, namespace };
	}

	/**
	 * Encapsulated background storage of user interaction, extracting attached documents if present.
	 */
	public async recordInteraction(
		userPrompt: string,
		references?: readonly vscode.ChatPromptReference[]
	): Promise<void> {
		if (!this.isEnabled() || !userPrompt.trim()) {
			return;
		}
		try {
			const attachmentText = await extractAttachmentText(references);
			const factToStore = attachmentText
				? `${userPrompt.trim()}\n\n${attachmentText}`
				: userPrompt.trim();

			await this.storeMemory({
				fact: factToStore,
				speaker: 'user',
			});
		} catch {
			// non-blocking fail-safe
		}
	}

	/**
	 * Encapsulated background storage of agent response.
	 */
	public async recordAgentResponse(agentResponse: string): Promise<void> {
		if (!this.isEnabled() || !agentResponse.trim()) {
			return;
		}
		try {
			await this.storeMemory({
				fact: agentResponse.trim(),
				speaker: 'agent',
			});
		} catch {
			// non-blocking fail-safe
		}
	}

	/**
	 * Renders the Reeve long-term memory attribution badge.
	 */
	public renderMemoryCitation(stream: vscode.ChatResponseStream, namespace: string): void {
		try {
			stream.markdown(new vscode.MarkdownString(`\n\n---\n*🧠 Recalled from Reeve Long-Term Memory (\`${namespace}\`)*`));
		} catch {
			// fail-safe
		}
	}

	/**
	 * Action Observer & Human-Centered Explanation Layer:
	 */
	public startActionObservation(sessionId: string, stream?: vscode.ChatResponseStream, userRequest = '', model?: vscode.LanguageModelChat): ISessionActionObserver {
		return this.explanationLayer.startSession(sessionId, stream, userRequest, model);
	}

	public async onBeforeAction(
		event: ReeveActionEvent
	): Promise<{ action: any; preExplanation?: string } | undefined> {
		const recalled = event.sessionId ? this.lastRecalledMemories.get(event.sessionId) || [] : [];
		return await this.explanationLayer.onBeforeAction(event, recalled);
	}

	public onAfterAction(
		event: ReeveActionEvent
	): void {
		this.explanationLayer.onAfterAction(event);
	}

	public async onBeforeToolAction(
		toolName: string,
		input: any,
		sessionId?: string
	): Promise<{ action: any; preExplanation?: string } | undefined> {
		const recalled = sessionId ? this.lastRecalledMemories.get(sessionId) || [] : [];
		return await this.explanationLayer.onBeforeToolAction(toolName, input, sessionId, recalled);
	}

	public onAfterToolAction(
		actionId: string,
		result?: any,
		success: boolean = true,
		sessionId?: string
	): void {
		this.explanationLayer.onAfterToolAction(actionId, result, success, sessionId);
	}

	public recordToolAction(
		toolName: string,
		input: any,
		result?: any,
		success: boolean = true,
		sessionId?: string
	): void {
		this.explanationLayer.recordToolAction(toolName, input, result, success, sessionId);
	}

	public async finalizeActionObservation(
		sessionId: string,
		agentResponseText: string,
		stream?: vscode.ChatResponseStream
	): Promise<ActionExplanation | undefined> {
		const recalled = this.lastRecalledMemories.get(sessionId) || [];
		try {
			return await this.explanationLayer.finalizeSession(
				sessionId,
				agentResponseText,
				stream,
				recalled
			);
		} finally {
			this.lastRecalledMemories.delete(sessionId);
		}
	}
}
