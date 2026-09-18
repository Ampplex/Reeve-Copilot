/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

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

const DEFAULT_ENDPOINT = 'https://mcp.reeve.co.in';
const DEFAULT_TIMEOUT_MS = 5000;
const CONFIG_SECTION = 'github.copilot.reeve';

export class ReeveClient implements IReeveClient {
	readonly _serviceBrand: undefined;

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
		} catch {
			// ignore
		}
		return process.env['REEVE_API_KEY'];
	}

	private getTimeoutMs(): number {
		try {
			const config = vscode.workspace.getConfiguration(CONFIG_SECTION);
			return config.get<number>('timeoutMs', DEFAULT_TIMEOUT_MS) || DEFAULT_TIMEOUT_MS;
		} catch {
			return DEFAULT_TIMEOUT_MS;
		}
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

		const endpoint = this.getEndpoint().replace(/\/+$/, '');
		const queryUrl = `${endpoint}/api/v1/memory/query`;
		const timeoutMs = this.getTimeoutMs();

		this.logService.debug(`[ReeveClient] Querying Reeve memory: query="${params.query}", namespace="${namespace}"`);

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
				const errorMsg = `HTTP ${response.status}: ${response.statusText}`;
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

		const endpoint = this.getEndpoint().replace(/\/+$/, '');
		const storeUrl = `${endpoint}/api/v1/memory/store`;
		const timeoutMs = this.getTimeoutMs();

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
				return { success: false, error: `HTTP ${response.status}: ${response.statusText}` };
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
}
