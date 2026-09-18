/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import * as vscode from 'vscode';
import { ActionContext } from '../common/reeveActionObserver';

export const HUMAN_EXPLANATION_PROMPT = `Explain this coding-agent action to the developer in simple, natural language. Use only the supplied evidence. Before execution, explain the intended change and why the evidence supports it. After execution, explain what actually happened and its practical effect. Do not invent reasoning. Do not repeat commands, diffs, or tool syntax. Do not explain implementation syntax unless necessary. Be concise and natural. If an existing explanation already covers the change, avoid repeating it.`;

export interface IHumanExplanationModel {
	explain(context: ActionContext, model?: vscode.LanguageModelChat): Promise<string | undefined>;
}

export class HumanExplanationService implements IHumanExplanationModel {
	async explain(context: ActionContext, selectedModel?: vscode.LanguageModelChat): Promise<string | undefined> {
		try {
			const models = await vscode.lm.selectChatModels({ vendor: 'copilot' });
			const model = selectedModel || models[0] || (await vscode.lm.selectChatModels())[0];
			if (!model) {
				return undefined;
			}
			const boundedContext = limitContext(context);

			const response = await model.sendRequest([
				vscode.LanguageModelChatMessage.User(HUMAN_EXPLANATION_PROMPT),
				vscode.LanguageModelChatMessage.User(JSON.stringify(boundedContext)),
			]);
			let explanation = '';
			for await (const text of response.text) {
				explanation += text;
			}
			return explanation.trim() || undefined;
		} catch {
			return undefined;
		}
	}
}

const MAX_DIFF = 12000;
const MAX_RESULT = 8000;
const MAX_MEMORY = 6000;
const MAX_CONTENT = 12000;
const MAX_COMMAND = 4000;

function limitContext(context: ActionContext): ActionContext {
	return {
		...context,
		command: truncate(context.command, MAX_COMMAND),
		diff: truncate(context.diff, MAX_DIFF),
		content: truncate(context.content, MAX_CONTENT),
		result: typeof context.result === 'string' ? truncate(context.result, MAX_RESULT) : context.result,
		reeveMemory: truncate(context.reeveMemory, MAX_MEMORY),
		agentResponse: truncate(context.agentResponse, MAX_RESULT),
		priorExplanation: truncate(context.priorExplanation, MAX_RESULT),
		actions: context.actions?.map(action => ({
			...action,
			command: truncate(action.command, MAX_COMMAND),
			diff: truncate(action.diff, MAX_DIFF),
			content: truncate(action.content, MAX_CONTENT),
			result: typeof action.result === 'string' ? truncate(action.result, MAX_RESULT) : action.result,
		})),
	};
}

function truncate(value: string | undefined, maxLength: number): string | undefined {
	return value && value.length > maxLength ? `${value.slice(0, maxLength)}\n[truncated]` : value;
}
