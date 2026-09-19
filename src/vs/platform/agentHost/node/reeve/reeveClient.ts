/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import * as fs from 'fs';
import { join } from '../../../../base/common/path.js';
import { ILogService } from '../../../log/common/log.js';

export interface ReeveMemoryItem {
	readonly id: string;
	readonly content: string;
	readonly score?: number;
	readonly timestamp?: string;
	readonly metadata?: Record<string, unknown>;
	readonly supersededBy?: string;
	readonly isHistorical?: boolean;
}

export interface ReeveSearchParams {
	readonly query: string;
	readonly limit?: number;
	readonly namespace?: string;
}

export interface ReeveSearchResult {
	readonly memories: readonly ReeveMemoryItem[];
	readonly count: number;
	readonly namespace?: string;
}

export interface ReeveStoreParams {
	readonly content: string;
	readonly type?: string;
	readonly metadata?: Record<string, unknown>;
	readonly namespace?: string;
}

export interface ReeveStoreResult {
	readonly success: boolean;
	readonly memoryId?: string;
	readonly error?: string;
}

export interface IReeveClient {
	search(query: string, limit?: number): Promise<ReeveSearchResult>;
	store(content: string, type?: string, metadata?: Record<string, unknown>): Promise<ReeveStoreResult>;
}

const DEFAULT_ENDPOINT = 'https://mcp.reeve.co.in';
const DEFAULT_TIMEOUT_MS = 6000;

export class ReeveClient implements IReeveClient {
	private readonly endpoint: string;
	private apiKey: string;

	constructor(@ILogService private readonly logService?: ILogService) {
		this.endpoint = process.env.REEVE_ENDPOINT || DEFAULT_ENDPOINT;
		this.apiKey = process.env.REEVE_API_KEY || '';
		if (!this.apiKey) {
			this.apiKey = this.tryReadApiKeyFromEnvFile();
		}
	}

	private tryReadApiKeyFromEnvFile(): string {
		try {
			const candidatePaths = [
				join(process.cwd(), 'extensions', 'copilot', '.env'),
				join(process.cwd(), '.env'),
			];
			for (const p of candidatePaths) {
				if (fs.existsSync(p)) {
					const content = fs.readFileSync(p, 'utf8');
					const match = content.match(/^REEVE_API_KEY=(.+)$/m);
					if (match && match[1]) {
						const key = match[1].trim();
						if (key) {
							return key;
						}
					}
				}
			}
		} catch {
			// ignore file read errors
		}
		return '';
	}

	hasApiKey(): boolean {
		return Boolean(this.apiKey) && !this.isTestEnvironment();
	}

	private isTestEnvironment(): boolean {
		return Boolean(
			process.env.VSCODE_UNIT_TESTS ||
			process.argv.some(arg => arg.includes('test/unit') || arg.includes('mocha'))
		);
	}

	async search(query: string, limit: number = 5): Promise<ReeveSearchResult> {
		if (!this.hasApiKey() || !query || query.trim().length === 0) {
			return { memories: [], count: 0 };
		}
		try {
			const controller = new AbortController();
			const timeout = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);

			const headers: Record<string, string> = {
				'Content-Type': 'application/json',
			};
			if (this.apiKey) {
				headers['Authorization'] = `Bearer ${this.apiKey}`;
			}

			const response = await fetch(`${this.endpoint}/search`, {
				method: 'POST',
				headers,
				body: JSON.stringify({ query, limit }),
				signal: controller.signal,
			}).finally(() => clearTimeout(timeout));

			if (!response.ok) {
				this.logService?.warn(`[ReeveClient] search returned HTTP ${response.status}`);
				return { memories: [], count: 0 };
			}

			const data: any = await response.json();
			const memories: ReeveMemoryItem[] = (data.memories || data.results || []).map((item: any) => ({
				id: item.id || item.memoryId || String(Math.random()),
				content: item.content || item.text || '',
				score: typeof item.score === 'number' ? item.score : undefined,
				timestamp: item.timestamp,
				metadata: item.metadata,
				isHistorical: Boolean(item.isHistorical || item.supersededBy),
				supersededBy: item.supersededBy,
			}));

			return {
				memories,
				count: memories.length,
				namespace: data.namespace,
			};
		} catch (err) {
			this.logService?.warn(`[ReeveClient] search failed (fail-safe fallback): ${err}`);
			return { memories: [], count: 0 };
		}
	}

	async store(content: string, type: string = 'fact', metadata?: Record<string, unknown>): Promise<ReeveStoreResult> {
		if (!content || content.trim().length === 0) {
			return { success: false, error: 'Empty content' };
		}
		try {
			const controller = new AbortController();
			const timeout = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);

			const headers: Record<string, string> = {
				'Content-Type': 'application/json',
			};
			if (this.apiKey) {
				headers['Authorization'] = `Bearer ${this.apiKey}`;
			}

			const response = await fetch(`${this.endpoint}/store`, {
				method: 'POST',
				headers,
				body: JSON.stringify({ content, type, metadata }),
				signal: controller.signal,
			}).finally(() => clearTimeout(timeout));

			if (!response.ok) {
				return { success: false, error: `HTTP ${response.status}` };
			}

			const data: any = await response.json();
			return { success: true, memoryId: data.id || data.memoryId };
		} catch (err: any) {
			this.logService?.warn(`[ReeveClient] store failed (fail-safe fallback): ${err}`);
			return { success: false, error: String(err) };
		}
	}

	static formatMemoriesForContext(memories: readonly ReeveMemoryItem[]): string {
		if (!memories || memories.length === 0) {
			return '';
		}
		const lines = [
			'### 🧠 Reeve Project Memory (Ground Truth Architecture & Constraints)',
			'The following verified project memories and architecture rules apply to this codebase:',
		];
		for (const mem of memories) {
			const prefix = mem.isHistorical ? '*(Historical / Superseded)* ' : '';
			lines.push(`- ${prefix}${mem.content}`);
		}
		return lines.join('\n');
	}
}
