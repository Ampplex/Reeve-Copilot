/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import * as l10n from '@vscode/l10n';
import type * as vscode from 'vscode';
import { IReeveClient } from '../../../platform/reeve/common/reeveClient';
import { CancellationToken } from '../../../util/vs/base/common/cancellation';
import { LanguageModelTextPart, LanguageModelToolResult } from '../../../vscodeTypes';
import { ToolName } from '../common/toolNames';
import { ToolRegistry } from '../common/toolsRegistry';
import { checkCancellation } from './toolUtils';

export interface IReeveSearchMemoryParams {
	query: string;
	namespace?: string;
	category?: string;
	limit?: number;
}

class ReeveSearchMemoryTool implements vscode.LanguageModelTool<IReeveSearchMemoryParams> {
	public static readonly toolName = ToolName.ReeveSearchMemory;

	constructor(
		@IReeveClient private readonly reeveClient: IReeveClient,
	) { }

	async invoke(
		options: vscode.LanguageModelToolInvocationOptions<IReeveSearchMemoryParams>,
		token: CancellationToken
	): Promise<vscode.LanguageModelToolResult> {
		checkCancellation(token);

		const { query, namespace, category, limit } = options.input;

		try {
			const result = await this.reeveClient.queryMemory({
				query,
				namespace,
				category,
				limit: limit ?? 5,
			}, token);

			if (!result.success || result.items.length === 0) {
				const message = result.error
					? `Reeve temporal memory unavailable (${result.error}). Proceeding with normal Copilot flow.`
					: `No relevant Reeve project memory found for "${query}" in namespace "${result.namespace}".`;
				return new LanguageModelToolResult([new LanguageModelTextPart(message)]);
			}

			const parts: string[] = [];

			if (result.answer) {
				parts.push(`**Synthesized Knowledge Graph Answer:**\n${result.answer}`);
			}

			const memoryEntries = result.items.map((item, idx) => {
				const header = `[Record ${idx + 1}${item.category ? ` • ${item.category}` : ''}]`;
				const tags = item.tags && item.tags.length > 0 ? ` [Tags: ${item.tags.join(', ')}]` : '';
				const time = item.timestamp ? ` [Recorded: ${item.timestamp}]` : '';
				const superseded = item.supersededBy ? ` [Superseded by: ${item.supersededBy}]` : '';
				return `${header}${tags}${time}${superseded}\n${item.content}`;
			}).join('\n\n---\n\n');

			parts.push(`**Retrieved Facts from Temporal Knowledge Graph (namespace: "${result.namespace}"):**\n\n${memoryEntries}`);

			const responseText = parts.join('\n\n');
			return new LanguageModelToolResult([new LanguageModelTextPart(responseText)]);
		} catch (error: any) {
			// Fail-safe: Tool error must NEVER break normal Copilot flow
			return new LanguageModelToolResult([
				new LanguageModelTextPart(`Reeve memory retrieval encountered an error: ${error?.message ?? String(error)}. Proceeding with normal Copilot execution.`)
			]);
		}
	}

	prepareInvocation(
		options: vscode.LanguageModelToolInvocationPrepareOptions<IReeveSearchMemoryParams>,
		_token: vscode.CancellationToken
	): vscode.ProviderResult<vscode.PreparedToolInvocation> {
		const query = options.input.query ? `"${options.input.query}"` : '';
		return {
			invocationMessage: l10n.t`Searching Reeve project memory for ${query}`,
			pastTenseMessage: l10n.t`Searched Reeve project memory for ${query}`
		};
	}
}

ToolRegistry.registerTool(ReeveSearchMemoryTool);
