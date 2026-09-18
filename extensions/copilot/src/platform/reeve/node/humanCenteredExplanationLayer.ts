/*--------------------------------------------------------------------------------
 * Copyright (c) 2026 Ankesh Kumar.
 * Licensed under the MIT License. See License.txt in the project root for
 * license information.
 *--------------------------------------------------------------------------------*/

import * as vscode from 'vscode';
import { ActionContext, ActionEvidence, ActionExplanation, ISessionActionObserver, ObservedAction } from '../common/reeveActionObserver';
import { ReeveMemoryItem } from '../common/reeveClient';
import { HumanExplanationService, IHumanExplanationModel } from './humanExplanationService';
import { SessionActionObserver } from './reeveActionObserver';

export interface IExplanationStream {
	markdown(value: string): void;
	progress?(value: string): void;
}

export class HumanCenteredExplanationLayer {
	private readonly activeObservers = new Map<string, SessionActionObserver>();
	private readonly sessionStreams = new Map<string, IExplanationStream>();
	private readonly sessionMemories = new Map<string, readonly ReeveMemoryItem[]>();
	private readonly preExplanations = new Map<string, string>();
	private readonly sessionModels = new Map<string, vscode.LanguageModelChat>();

	constructor(private readonly explanationService: IHumanExplanationModel = new HumanExplanationService()) { }

	startSession(sessionId: string, stream?: IExplanationStream, userRequest = '', model?: vscode.LanguageModelChat): ISessionActionObserver {
		const observer = new SessionActionObserver(sessionId, userRequest);
		this.activeObservers.set(sessionId, observer);
		if (stream) {
			this.sessionStreams.set(sessionId, stream);
		}
		this.sessionMemories.set(sessionId, []);
		if (model) {
			this.sessionModels.set(sessionId, model);
		}
		return observer;
	}

	getSessionObserver(sessionId: string): SessionActionObserver | undefined {
		return this.activeObservers.get(sessionId);
	}

	async onBeforeToolAction(toolName: string, input: any, sessionId?: string, recalledMemories: readonly ReeveMemoryItem[] = []): Promise<{ action: ObservedAction; preExplanation?: string } | undefined> {
		const targetSessionId = sessionId || (this.activeObservers.size === 1 ? this.activeObservers.keys().next().value : undefined);
		if (!targetSessionId) {
			return undefined;
		}
		const observer = this.activeObservers.get(targetSessionId);
		if (!observer) {
			return undefined;
		}

		const { action } = observer.recordBeforeToolInvocation(toolName, input, recalledMemories);
		this.sessionMemories.set(targetSessionId, recalledMemories);
		if (!observer.isMeaningfulAction(action)) {
			return { action };
		}
		const explanation = await this.explanationService.explain(this.buildContext(observer, action, recalledMemories, 'before'), this.sessionModels.get(targetSessionId));
		if (explanation) {
			this.preExplanations.set(targetSessionId, explanation);
		}
		this.render(explanation, targetSessionId);
		return { action, preExplanation: explanation };
	}

	onAfterToolAction(actionId: string, result: any, success: boolean, sessionId?: string): void {
		const targetSessionId = sessionId || (this.activeObservers.size === 1 ? this.activeObservers.keys().next().value : undefined);
		if (targetSessionId) {
			this.activeObservers.get(targetSessionId)?.recordAfterToolInvocation(actionId, result, success);
		}
	}

	recordToolAction(toolName: string, input: any, result: any, success: boolean, sessionId?: string): void {
		const targetSessionId = sessionId || (this.activeObservers.size === 1 ? this.activeObservers.keys().next().value : undefined);
		if (targetSessionId) {
			this.activeObservers.get(targetSessionId)?.recordToolInvocation(toolName, input, result, success);
		}
	}

	async finalizeSession(sessionId: string, agentResponseText: string, stream?: IExplanationStream, recalledMemories: readonly ReeveMemoryItem[] = []): Promise<ActionExplanation | undefined> {
		const observer = this.activeObservers.get(sessionId);
		if (!observer) {
			return undefined;
		}
		try {
			const actions = observer.getActions().filter(candidate => observer.isMeaningfulAction(candidate));
			if (actions.length === 0) {
				return undefined;
			}
			const explanation = await this.explanationService.explain(this.buildContext(observer, actions[actions.length - 1], this.sessionMemories.get(sessionId) || recalledMemories, 'after', agentResponseText, this.preExplanations.get(sessionId), actions), this.sessionModels.get(sessionId));
			if (!explanation) {
				return undefined;
			}
			const result: ActionExplanation = {
				summary: explanation,
			};
			const targetStream = stream || this.sessionStreams.get(sessionId);
			if (targetStream) {
				try { targetStream.markdown(`\n\n${explanation}\n\n`); } catch { /* fail-safe */ }
			}
			return result;
		} finally {
			this.activeObservers.delete(sessionId);
			this.sessionStreams.delete(sessionId);
			this.sessionMemories.delete(sessionId);
			this.preExplanations.delete(sessionId);
			this.sessionModels.delete(sessionId);
		}
	}

	private buildContext(observer: SessionActionObserver, action: ObservedAction | undefined, memories: readonly ReeveMemoryItem[], phase: ActionContext['phase'], agentResponse?: string, priorExplanation?: string, actions: readonly ObservedAction[] = [action].filter((candidate): candidate is ObservedAction => !!candidate)): ActionContext {
		const details = action?.details || {};
		const memory = memories.map(item => `${item.supersededBy || item.validTo ? 'Historical (superseded): ' : ''}${item.content}`).join('\n');
		return {
			phase,
			type: this.actionType(action),
			target: action?.targetResource,
			command: details.command,
			diff: details.diff,
			content: details.content,
			result: details.result,
			userRequest: observer.getUserRequest(),
			reeveMemory: memory || undefined,
			agentResponse,
			priorExplanation,
			actions: actions.map(candidate => this.actionEvidence(candidate)),
		};
	}

	private actionEvidence(action: ObservedAction): ActionEvidence {
		return {
			type: this.actionType(action),
			target: action.targetResource,
			command: action.details?.command,
			diff: action.details?.diff,
			content: action.details?.content,
			result: action.details?.result,
			success: action.success,
		};
	}

	private actionType(action: ObservedAction | undefined): ActionContext['type'] {
		switch (action?.category) {
			case 'file_edit': return 'edit';
			case 'file_create': return 'create';
			case 'file_delete': return 'delete';
			case 'shell_command': return 'command';
			case 'test_run': return 'test';
			default: return 'other';
		}
	}

	private render(explanation: string | undefined, sessionId: string): void {
		if (!explanation) {
			return;
		}
		try { this.sessionStreams.get(sessionId)?.markdown(`\n\n${explanation}\n\n`); } catch { /* fail-safe */ }
	}

}
